import ast
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app import v22_commercial as auth


ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_SECRET = "synthetic-environment-secret-for-offline-tests"


@pytest.mark.parametrize("name", ["SESSION_SECRET", "PROTREBOT_SESSION_SECRET"])
def test_session_secret_reads_environment_without_using_local_file(name):
    with patch.dict(os.environ, {name: SYNTHETIC_SECRET}, clear=True), patch.object(auth, "SECRET_PATH") as path:
        assert auth.load_secret() == hashlib.sha256(SYNTHETIC_SECRET.encode()).digest()
        assert auth.has_stable_session_secret()
        path.exists.assert_not_called()


@pytest.mark.parametrize("name", ["SESSION_SECRET", "PROTREBOT_SESSION_SECRET"])
def test_short_configured_secret_fails_instead_of_falling_back(name):
    with patch.dict(os.environ, {name: "synthetic-short", "PROTREBOT_WEB_ACCESS_TOKEN": SYNTHETIC_SECRET}, clear=True):
        with pytest.raises(RuntimeError, match="en az 32 karakter"):
            auth.load_secret()
        with pytest.raises(RuntimeError, match="en az 32 karakter"):
            auth.has_stable_session_secret()


def test_conflicting_session_secret_aliases_fail_without_disclosing_values():
    values = {"SESSION_SECRET": SYNTHETIC_SECRET, "PROTREBOT_SESSION_SECRET": SYNTHETIC_SECRET + "-other"}
    with patch.dict(os.environ, values, clear=True):
        with pytest.raises(RuntimeError, match="farklı") as error:
            auth.load_secret()
    for value in values.values():
        assert value not in str(error.value)


def test_identical_aliases_preserve_compatibility():
    with patch.dict(os.environ, {"SESSION_SECRET": SYNTHETIC_SECRET, "PROTREBOT_SESSION_SECRET": SYNTHETIC_SECRET}, clear=True):
        assert auth.load_secret() == hashlib.sha256(SYNTHETIC_SECRET.encode()).digest()


def test_missing_durable_secret_fails_without_generating_local_key():
    with patch.dict(os.environ, {}, clear=True), patch.object(auth, "DURABLE_AUTH_REQUIRED", True), patch.object(auth, "SECRET_PATH") as path:
        assert not auth.has_stable_session_secret()
        with pytest.raises(RuntimeError, match="Kalıcı oturum anahtarı eksik"):
            auth.load_secret()
        path.exists.assert_not_called()


def test_legacy_owner_token_derivation_is_unchanged():
    with patch.dict(os.environ, {"PROTREBOT_WEB_ACCESS_TOKEN": SYNTHETIC_SECRET}, clear=True):
        assert auth.load_secret() == hashlib.sha256(f"protrebot-v22-session-v1:{SYNTHETIC_SECRET}".encode()).digest()
        assert auth.has_stable_session_secret()


def test_env_example_is_blank_and_never_exposes_secrets_to_browser():
    keys = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        assert not value.strip()
        keys.add(key)
    assert {"DATABASE_URL", "SESSION_SECRET", "PROTREBOT_SESSION_SECRET", "ANTHROPIC_API_KEY"} <= keys
    assert not any(
        key.startswith("VITE_") and any(word in key for word in ("SECRET", "PASSWORD", "TOKEN", "DATABASE", "PRIVATE", "API_KEY"))
        for key in keys
    )


def test_legacy_database_config_has_no_literal_credential_fallback():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    assignment = next(
        node for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DATABASE_URL" for target in node.targets)
    )
    assert not any(
        isinstance(node, ast.Constant) and isinstance(node.value, str) and "://" in node.value
        for node in ast.walk(assignment)
    )
    statements = [assignment, tree.body[tree.body.index(assignment) + 1]]
    namespace = {"os": os}
    with patch.dict(os.environ, {}, clear=True), pytest.raises(RuntimeError, match="DATABASE_URL"):
        exec(compile(ast.Module(body=statements, type_ignores=[]), "database_config", "exec"), namespace)
    with patch.dict(os.environ, {"DATABASE_URL": "synthetic-configured-database-url"}, clear=True):
        exec(compile(ast.Module(body=statements, type_ignores=[]), "database_config", "exec"), namespace)
        assert namespace["DATABASE_URL"] == "synthetic-configured-database-url"
