import unittest

from chagent.models.factory import create_model_client
from chagent.models.settings import ModelConfigurationError, ModelSettings, PRESETS
from chagent.models.anthropic import AnthropicClient
from chagent.models.openai_chat import OpenAIChatClient


class ModelSettingsTests(unittest.TestCase):
    def test_each_named_key_is_sufficient(self):
        for name, preset in PRESETS.items():
            with self.subTest(provider=name):
                settings = ModelSettings.from_env({preset.key_env: "unit-test-key"})
                self.assertEqual(settings.provider, name)
                self.assertEqual(settings.model_id, preset.model)
                self.assertEqual(settings.base_url, preset.base_url)
                self.assertNotIn("unit-test-key", repr(settings))
                self.assertIsInstance(create_model_client(settings), AnthropicClient if name == "anthropic" else OpenAIChatClient)

    def test_multiple_or_unknown_keys_do_not_trigger_credential_guessing(self):
        for env in ({}, {"CHAGENT_API_KEY": "sk-anything"},
                    {"OPENAI_API_KEY": "one", "DEEPSEEK_API_KEY": "two"}):
            with self.subTest(env=list(env)), self.assertRaises(ModelConfigurationError):
                ModelSettings.from_env(env)
        settings = ModelSettings.from_env({"CHAGENT_PROVIDER": "deepseek", "OPENAI_API_KEY": "one", "DEEPSEEK_API_KEY": "two"})
        self.assertEqual(settings.api_key, "two")
        selected = ModelSettings.from_env({"OPENAI_API_KEY": "named-key", "CHAGENT_API_KEY": "unclassified-key"})
        self.assertEqual(selected.api_key, "named-key")

    def test_custom_model_and_base_url_overrides(self):
        settings = ModelSettings.from_env({
            "CHAGENT_PROVIDER": "custom", "CHAGENT_API_KEY": "local-test",
            "CHAGENT_BASE_URL": "http://127.0.0.1:8080/v1/", "CHAGENT_MODEL": "local-model",
            "CHAGENT_CONTEXT_WINDOW": "16384", "CHAGENT_MAX_TOKENS": "2048",
        })
        self.assertEqual(settings.base_url, "http://127.0.0.1:8080/v1")
        self.assertEqual(settings.model().id, "local-model")
        self.assertEqual(settings.model().max_tokens, 2048)

    def test_invalid_settings_fail_without_exposing_credentials(self):
        for override in (
            {"CHAGENT_PROVIDER": "unknown"},
            {"CHAGENT_BASE_URL": "https://user:private-secret@example.com/v1"},
            {"CHAGENT_BASE_URL": "https://example.com/v1?key=private-secret"},
            {"CHAGENT_BASE_URL": "http://example.com/v1"},
            {"CHAGENT_CONTEXT_WINDOW": "1"}, {"CHAGENT_MAX_TOKENS": "not-a-number"},
            {"CHAGENT_MODEL_TIMEOUT": "nan"}, {"CHAGENT_RUN_TIMEOUT": "0"},
        ):
            with self.subTest(fields=list(override)), self.assertRaises(ModelConfigurationError) as raised:
                ModelSettings.from_env({"OPENAI_API_KEY": "private-secret", **override})
            self.assertNotIn("private-secret", str(raised.exception))
