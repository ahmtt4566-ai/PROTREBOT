import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from tools.verify_chart_ai_postgres import (
    LocalVerificationError,
    console_password,
    ignore_database_environment,
    record_error,
    validate_target,
    verify_connection,
)


@pytest.mark.parametrize("host,port,database", [
    ("remote.example", 5432, "chart_ai_stage3_" + "a" * 32),
    ("0.0.0.0", 5432, "chart_ai_stage3_" + "a" * 32),
    ("127.0.0.1", 5433, "chart_ai_stage3_" + "a" * 32),
    ("127.0.0.1", 5432, "production"),
    ("127.0.0.1", 5432, "chart_ai_stage3_unsafe;DROP"),
    ("127.0.0.1", 5432, "postgres"),
])
def test_only_literal_loopback_and_fresh_database_names_are_allowed(host, port, database):
    with pytest.raises(ValueError):
        validate_target(host, port, database)


def test_explicit_administrative_connection_is_only_for_local_postgres():
    validate_target("127.0.0.1", 5432, "postgres", administrative=True)
    validate_target("127.0.0.1", 5432, "chart_ai_stage3_" + "a" * 32)
    with pytest.raises(ValueError):
        validate_target("remote.example", 5432, "postgres", administrative=True)


def test_database_environment_is_never_used():
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://remote.example/forbidden", "PGPASSWORD": "test-only"}):
        ignore_database_environment()
        assert "DATABASE_URL" not in os.environ
        assert "PGPASSWORD" not in os.environ


def test_noninteractive_password_fallback_is_forbidden():
    with patch("sys.stdin.isatty", return_value=False), patch("getpass.getpass") as prompt:
        with pytest.raises(RuntimeError):
            console_password()
        prompt.assert_not_called()


def test_ip_comparison_uses_host_not_inet_text_with_network_mask():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"host": "127.0.0.1", "port": 5432, "database": "postgres"}
    conn.fetchval.return_value = "localhost"
    asyncio.run(verify_connection(conn, "postgres"))
    assert "host(inet_server_addr())" in conn.fetchrow.call_args.args[0]
    assert "inet_server_addr()::text AS host" not in conn.fetchrow.call_args.args[0]


@pytest.mark.parametrize("field,value,code", [
    ("host", "10.0.0.1", "server_address_mismatch"),
    ("port", 5433, "server_port_mismatch"),
    ("database", "wrong_database", "database_mismatch"),
])
def test_identity_failures_have_specific_codes_without_disclosing_unexpected_targets(field, value, code):
    conn = AsyncMock()
    conn.fetchrow.return_value = {"host": "127.0.0.1", "port": 5432, "database": "postgres", field: value}
    with pytest.raises(LocalVerificationError) as failure:
        asyncio.run(verify_connection(conn, "postgres"))
    assert record_error(failure.value)["code"] == code
    assert str(value) not in record_error(failure.value)["message"]


def test_running_configuration_mismatch_is_separate_from_password_errors():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"host": "127.0.0.1", "port": 5432, "database": "postgres"}
    conn.fetchval.return_value = "*"
    with pytest.raises(LocalVerificationError) as failure:
        asyncio.run(verify_connection(conn, "postgres"))
    assert failure.value.code == "runtime_listen_mismatch"


@pytest.mark.parametrize("error,code", [
    (asyncpg.InvalidPasswordError("test-super-secret"), "authentication_failed"),
    (ConnectionRefusedError("test-super-secret"), "connection_refused"),
    (TimeoutError("test-super-secret"), "connection_timeout"),
])
def test_connection_error_classification_never_logs_raw_error_or_password(error, code):
    result = record_error(error)
    assert result["code"] == code
    assert "test-super-secret" not in json.dumps(result)
