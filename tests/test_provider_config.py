import os
import unittest
from unittest import mock

from provider_config import ProviderConfig, normalize_api_mode, normalize_provider_config
from agentmain import GenericAgent


class ProviderConfigTests(unittest.TestCase):
    def test_missing_key_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
            ProviderConfig.from_env(
                name="deepseek", prefix="DEEPSEEK",
                default_api_base="https://api.deepseek.com/v1",
                default_model="deepseek-chat", environ={},
            )

    def test_environment_builds_typed_config_without_revealing_key(self):
        config = ProviderConfig.from_env(
            name="deepseek", prefix="DEEPSEEK",
            default_api_base="https://api.deepseek.com/v1",
            default_model="deepseek-chat",
            environ={"DEEPSEEK_API_KEY": "unit-secret", "DEEPSEEK_API_MODE": "responses"},
        )
        self.assertEqual(config.api_mode, "responses")
        self.assertEqual(config.session_type, "native_oai")
        self.assertNotIn("unit-secret", repr(config))
        self.assertIn("tools", config.capabilities)

    def test_legacy_mapping_uses_credential_reference(self):
        with mock.patch.dict(os.environ, {"UNIT_PROVIDER_KEY": "resolved"}, clear=False):
            config = normalize_provider_config({
                "name": "unit", "apikey": "", "credential_env": "UNIT_PROVIDER_KEY",
                "apibase": "https://example.invalid/v1", "model": "unit",
                "api_mode": "chat-completions",
            })
        self.assertEqual(config["apikey"], "resolved")
        self.assertEqual(config["api_mode"], "chat_completions")

    def test_session_type_survives_legacy_conversion(self):
        config = ProviderConfig.from_env(
            name="unit", prefix="UNIT", default_api_base="https://example.invalid",
            default_model="unit", session_type="native_claude",
            environ={"UNIT_API_KEY": "unit-secret"},
        )
        self.assertEqual(config.to_legacy_dict()["session_type"], "native_claude")

    def test_unknown_api_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_api_mode("mystery")

    @mock.patch("agentmain.reload_mykeys", return_value=({}, True))
    def test_agent_reports_missing_providers_without_modulo_error(self, _reload):
        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        with self.assertRaisesRegex(RuntimeError, "No LLM providers configured"):
            agent.load_llm_sessions()


if __name__ == "__main__":
    unittest.main()
