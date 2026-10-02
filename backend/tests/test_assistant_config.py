import os
import re
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import anthropic
from app.assistant_config import AssistantConfig, load_assistant_config
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]


class AssistantConfigTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_defaults_and_missing_key_do_not_crash(self):
        config = load_assistant_config()
        self.assertEqual(config.model_dump(), {
            "enabled": True,
            "model": "claude-haiku-4-5-20251001",
            "max_input_chars": 500,
            "history_messages": 4,
            "history_message_max_chars": 2000,
            "page_context_max_chars": 300,
            "request_timeout_seconds": 30,
            "protection_stale_seconds": 30,
            "proactive_enabled": True,
            "proactive_inactive_days": 7,
            "proactive_cooldown_hours": 24,
            "proactive_poll_seconds": 300,
            "secret_min_alphanumeric_chars": 40,
            "budget_warning_fraction": Decimal("0.8"),
            "per_minute_limit": 6,
            "daily_limit": 20,
            "monthly_budget_usd": Decimal(50),
            "max_output_tokens": 500,
            "max_llm_calls_per_message": 3,
            "max_total_input_tokens_per_message": 12000,
            "max_total_output_tokens_per_message": 1500,
            "cache_write_price_usd_per_million": Decimal("1.25"),
            "cache_read_price_usd_per_million": Decimal("0.10"),
            "input_price_usd_per_million": Decimal(1),
            "output_price_usd_per_million": Decimal(5),
        })
        self.assertFalse(config.available)
        self.assertIn("kullanılamıyor", config.unavailable_reason)

    def test_all_settings_can_be_overridden_by_environment(self):
        values = {
            "ASSISTANT_ENABLED": "true",
            "ASSISTANT_MODEL": "another-model",
            "ANTHROPIC_API_KEY": " assistant-config-test-key-not-real ",
            "ASSISTANT_MAX_INPUT_CHARS": "700",
            "ASSISTANT_HISTORY_MESSAGES": "8",
            "ASSISTANT_HISTORY_MESSAGE_MAX_CHARS": "2100",
            "ASSISTANT_PAGE_CONTEXT_MAX_CHARS": "350",
            "ASSISTANT_REQUEST_TIMEOUT_SECONDS": "45",
            "ASSISTANT_PROTECTION_STALE_SECONDS": "60",
            "ASSISTANT_PROACTIVE_ENABLED": "false",
            "ASSISTANT_PROACTIVE_INACTIVE_DAYS": "14",
            "ASSISTANT_PROACTIVE_COOLDOWN_HOURS": "48",
            "ASSISTANT_PROACTIVE_POLL_SECONDS": "600",
            "ASSISTANT_SECRET_MIN_ALPHANUMERIC_CHARS": "50",
            "ASSISTANT_BUDGET_WARNING_FRACTION": "0.9",
            "ASSISTANT_PER_MINUTE_LIMIT": "9",
            "ASSISTANT_DAILY_LIMIT": "40",
            "ASSISTANT_MONTHLY_BUDGET_USD": "75.25",
            "ASSISTANT_MAX_OUTPUT_TOKENS": "600",
            "ASSISTANT_MAX_LLM_CALLS_PER_MESSAGE": "2",
            "ASSISTANT_MAX_TOTAL_INPUT_TOKENS_PER_MESSAGE": "8000",
            "ASSISTANT_MAX_TOTAL_OUTPUT_TOKENS_PER_MESSAGE": "1000",
            "ASSISTANT_CACHE_WRITE_PRICE_USD_PER_MILLION": "3.25",
            "ASSISTANT_CACHE_READ_PRICE_USD_PER_MILLION": "0.25",
            "ASSISTANT_INPUT_PRICE_USD_PER_MILLION": "2.75",
            "ASSISTANT_OUTPUT_PRICE_USD_PER_MILLION": "13.50",
        }
        with patch.dict(os.environ, values):
            config = load_assistant_config()
        self.assertTrue(config.available)
        self.assertIsNone(config.unavailable_reason)
        self.assertEqual(config.model, "another-model")
        self.assertEqual(config.api_key.get_secret_value(), values["ANTHROPIC_API_KEY"].strip())
        self.assertEqual(config.max_input_chars, 700)
        self.assertEqual(config.history_messages, 8)
        self.assertEqual(config.history_message_max_chars, 2100)
        self.assertEqual(config.page_context_max_chars, 350)
        self.assertEqual(config.request_timeout_seconds, 45)
        self.assertEqual(config.protection_stale_seconds, 60)
        self.assertFalse(config.proactive_enabled)
        self.assertEqual(config.proactive_inactive_days, 14)
        self.assertEqual(config.proactive_cooldown_hours, 48)
        self.assertEqual(config.proactive_poll_seconds, 600)
        self.assertEqual(config.secret_min_alphanumeric_chars, 50)
        self.assertEqual(config.budget_warning_fraction, Decimal("0.9"))
        self.assertEqual(config.per_minute_limit, 9)
        self.assertEqual(config.daily_limit, 40)
        self.assertEqual(config.monthly_budget_usd, Decimal("75.25"))
        self.assertEqual(config.max_output_tokens, 600)
        self.assertEqual(config.max_llm_calls_per_message, 2)
        self.assertEqual(config.max_total_input_tokens_per_message, 8000)
        self.assertEqual(config.max_total_output_tokens_per_message, 1000)
        self.assertEqual(config.cache_write_price_usd_per_million, Decimal("3.25"))
        self.assertEqual(config.cache_read_price_usd_per_million, Decimal("0.25"))
        self.assertEqual(config.input_price_usd_per_million, Decimal("2.75"))
        self.assertEqual(config.output_price_usd_per_million, Decimal("13.50"))

    def test_disabled_assistant_is_unavailable_even_with_a_key(self):
        with patch.dict(os.environ, {"ASSISTANT_ENABLED": "false", "ANTHROPIC_API_KEY": "assistant-config-test-key-not-real"}):
            config = load_assistant_config()
        self.assertFalse(config.enabled)
        self.assertFalse(config.available)
        self.assertIn("devre dışı", config.unavailable_reason)

    def test_blank_keys_are_unavailable(self):
        for key in ("", "   "):
            with self.subTest(key=key), patch.dict(os.environ, {"ANTHROPIC_API_KEY": key}):
                config = load_assistant_config()
                self.assertFalse(config.available)
                self.assertIn("kullanılamıyor", config.unavailable_reason)

    def test_model_switch_only_requires_the_model_setting(self):
        before = load_assistant_config().model_dump()
        with patch.dict(os.environ, {"ASSISTANT_MODEL": "another-model"}):
            after = load_assistant_config().model_dump()
        self.assertEqual(after, {**before, "model": "another-model"})

    def test_sdk_import_does_not_require_an_api_key(self):
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        self.assertTrue(callable(anthropic.AsyncAnthropic))
        self.assertFalse(load_assistant_config().available)

    def test_loaded_settings_cannot_be_mutated(self):
        config = load_assistant_config()
        with self.assertRaises(ValidationError):
            config.enabled = False
        self.assertTrue(config.enabled)

    def test_secret_is_not_exposed_in_repr_or_serialization_or_validation_errors(self):
        secret = "assistant-config-test-key-not-real"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}):
            config = load_assistant_config()
            self.assertNotIn(secret, repr(config))
            self.assertNotIn(secret, str(config))
            self.assertNotIn("api_key", config.model_dump())
            self.assertNotIn(secret, config.model_dump_json())
            with patch.dict(os.environ, {"ASSISTANT_MAX_INPUT_CHARS": secret}):
                with self.assertRaises(ValidationError) as raised:
                    load_assistant_config()
                self.assertNotIn(secret, str(raised.exception))

    def test_invalid_settings_raise_explicit_validation_errors(self):
        invalid = {
            "ASSISTANT_ENABLED": ("not-a-boolean",),
            "ASSISTANT_MODEL": ("", "   "),
            "ASSISTANT_MAX_INPUT_CHARS": ("0", "-1", "1.5", "not-a-number"),
            "ASSISTANT_HISTORY_MESSAGES": ("-1", "1.5"),
            "ASSISTANT_HISTORY_MESSAGE_MAX_CHARS": ("0", "-1"),
            "ASSISTANT_PAGE_CONTEXT_MAX_CHARS": ("0", "-1"),
            "ASSISTANT_REQUEST_TIMEOUT_SECONDS": ("0", "-1"),
            "ASSISTANT_PROTECTION_STALE_SECONDS": ("0", "29", "1.5"),
            "ASSISTANT_PROACTIVE_ENABLED": ("not-a-boolean",),
            "ASSISTANT_PROACTIVE_INACTIVE_DAYS": ("0", "-1", "1.5"),
            "ASSISTANT_PROACTIVE_COOLDOWN_HOURS": ("23", "-1", "1.5"),
            "ASSISTANT_PROACTIVE_POLL_SECONDS": ("59", "-1", "1.5"),
            "ASSISTANT_SECRET_MIN_ALPHANUMERIC_CHARS": ("0", "-1"),
            "ASSISTANT_BUDGET_WARNING_FRACTION": ("0", "1.1", "NaN", "Infinity"),
            "ASSISTANT_PER_MINUTE_LIMIT": ("0", "-1"),
            "ASSISTANT_DAILY_LIMIT": ("0", "-1"),
            "ASSISTANT_MONTHLY_BUDGET_USD": ("-1", "NaN", "Infinity"),
            "ASSISTANT_MAX_OUTPUT_TOKENS": ("0", "-1"),
            "ASSISTANT_MAX_LLM_CALLS_PER_MESSAGE": ("0", "4", "1.5"),
            "ASSISTANT_MAX_TOTAL_INPUT_TOKENS_PER_MESSAGE": ("0", "-1"),
            "ASSISTANT_MAX_TOTAL_OUTPUT_TOKENS_PER_MESSAGE": ("0", "-1"),
            "ASSISTANT_CACHE_WRITE_PRICE_USD_PER_MILLION": ("-1", "NaN", "Infinity"),
            "ASSISTANT_CACHE_READ_PRICE_USD_PER_MILLION": ("-1", "NaN", "Infinity"),
            "ASSISTANT_INPUT_PRICE_USD_PER_MILLION": ("-1", "NaN", "Infinity"),
            "ASSISTANT_OUTPUT_PRICE_USD_PER_MILLION": ("-1", "NaN", "Infinity"),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(setting=name, value=value), patch.dict(os.environ, {name: value}), self.assertRaises(ValidationError):
                    load_assistant_config()

    def test_zero_history_and_zero_budget_or_prices_are_valid(self):
        values = {
            "ASSISTANT_HISTORY_MESSAGES": "0",
            "ASSISTANT_MONTHLY_BUDGET_USD": "0",
            "ASSISTANT_INPUT_PRICE_USD_PER_MILLION": "0",
            "ASSISTANT_OUTPUT_PRICE_USD_PER_MILLION": "0",
        }
        with patch.dict(os.environ, values):
            config = load_assistant_config()
        self.assertEqual(config.history_messages, 0)
        self.assertEqual(config.monthly_budget_usd, Decimal(0))
        self.assertEqual(config.input_price_usd_per_million, Decimal(0))
        self.assertEqual(config.output_price_usd_per_million, Decimal(0))

    def test_render_and_environment_examples_match_config_defaults_without_a_secret(self):
        expected_names = {f"ASSISTANT_{name.upper()}" for name in AssistantConfig.model_fields if name != "api_key"}
        defaults = load_assistant_config().model_dump()
        render = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertRegex(render, r"- key: ANTHROPIC_API_KEY\r?\n\s+sync: false")
        self.assertNotRegex(render, r"- key: ANTHROPIC_API_KEY\r?\n\s+value:")
        values = {
            name: value.strip().strip('"')
            for name, value in re.findall(r"- key: (ASSISTANT_[A-Z_]+)\r?\n\s+value: ([^\r\n]+)", render)
        }
        self.assertEqual(set(values), expected_names)
        with patch.dict(os.environ, values):
            self.assertEqual(load_assistant_config().model_dump(), defaults)
        for filename in (".env.example", "env.example"):
            with self.subTest(filename=filename):
                example = (ROOT / filename).read_text(encoding="utf-8")
                self.assertRegex(example, r"(?m)^ANTHROPIC_API_KEY=\s*$")
                values = dict(re.findall(r"(?m)^(ASSISTANT_[A-Z_]+)=([^\r\n]*)", example))
                self.assertEqual(set(values), expected_names)
                with patch.dict(os.environ, values):
                    config = load_assistant_config()
                    self.assertEqual(config.model_dump(), defaults)
                    self.assertFalse(config.available)
