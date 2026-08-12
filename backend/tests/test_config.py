"""Settings tests.

Every Settings(...) here passes `_env_file=None`. Without it a developer's real
local .env leaks into the test run and defaults become unassertable.
"""

import pytest
from pydantic import ValidationError

from komora.config import Settings

REQUIRED = {
    "KOMORA_TELEGRAM_BOT_TOKEN": "123:ABC",
    "KOMORA_GEMINI_API_KEY": "gem-key",
    "KOMORA_TOKEN_ENCRYPTION_KEY": "enc-key",
    "KOMORA_PUBLIC_BASE_URL": "https://x.example",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_reads_required_values_and_applies_defaults(env: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    assert s.telegram_bot_token == "123:ABC"
    assert s.public_base_url == "https://x.example"
    assert s.silpo_mcp_url == "https://mcp.silpo.ua/mcp"
    assert s.database_url.startswith("sqlite+aiosqlite")
    assert s.llm_lite == "gemini/gemini-3.5-flash-lite"
    assert s.llm_full == "gemini/gemini-3.1-flash-lite"
    assert s.ollama_base_url == "http://localhost:11434"


def test_the_two_tiers_default_to_different_models(env: pytest.MonkeyPatch) -> None:
    """Free-tier quota is per (project, model) and a basket spends one request on each
    tier, so identical defaults would silently halve the baskets available per day."""
    s = Settings(_env_file=None)
    assert s.llm_lite != s.llm_full


def test_missing_required_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_trailing_slash_stripped_from_public_base_url(env: pytest.MonkeyPatch) -> None:
    """The OAuth callback is built by concatenation; a double slash breaks the redirect URI."""
    env.setenv("KOMORA_PUBLIC_BASE_URL", "https://x.example/")
    assert Settings(_env_file=None).public_base_url == "https://x.example"


class TestProviderValidation:
    def test_unknown_provider_in_a_tier_is_rejected(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("KOMORA_LLM_LITE", "openai/gpt-5")
        with pytest.raises(ValidationError, match="openai"):
            Settings(_env_file=None)

    def test_gemini_tier_requires_an_api_key(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("KOMORA_GEMINI_API_KEY", "")
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        message = str(excinfo.value)
        assert "llm_lite" in message, "the error must name which tier needs the key"
        assert "gemini_api_key" in message

    def test_all_ollama_config_boots_without_a_gemini_key(self, env: pytest.MonkeyPatch) -> None:
        """The local dev profile must work with no API key at all."""
        env.delenv("KOMORA_GEMINI_API_KEY", raising=False)
        env.setenv("KOMORA_LLM_LITE", "ollama/gemma4:12b")
        env.setenv("KOMORA_LLM_FULL", "ollama/gemma4:12b")
        s = Settings(_env_file=None)
        assert s.gemini_api_key == ""
        assert s.tier_ref("lite") == ("ollama", "gemma4:12b")

    def test_mixed_config_still_requires_the_key(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("KOMORA_GEMINI_API_KEY", "")
        env.setenv("KOMORA_LLM_LITE", "ollama/gemma4:12b")
        with pytest.raises(ValidationError, match="llm_full"):
            Settings(_env_file=None)


def test_tier_ref_returns_parsed_provider_and_model(env: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    assert s.tier_ref("lite") == ("gemini", "gemini-3.5-flash-lite")
    assert s.tier_ref("full") == ("gemini", "gemini-3.1-flash-lite")
