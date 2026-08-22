"""Settings tests.

Every Settings(...) here passes `_env_file=None`. Without it a developer's real
local .env leaks into the test run and defaults become unassertable.
"""

import re

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
    assert s.llm_agent == "gemini/gemini-3.5-flash-lite"
    assert s.llm_verifier == "gemini/gemini-3.1-flash-lite"
    assert s.ollama_base_url == "http://localhost:11434"


def test_the_two_roles_default_to_different_models(env: pytest.MonkeyPatch) -> None:
    """Free-tier quota is per (project, model) and a basket spends one request on each
    tier, so identical defaults would silently halve the baskets available per day."""
    s = Settings(_env_file=None)
    assert s.llm_agent != s.llm_verifier


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
    def test_unknown_provider_in_a_role_is_rejected(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("KOMORA_LLM_AGENT", "openai/gpt-5")
        with pytest.raises(ValidationError, match="openai"):
            Settings(_env_file=None)

    def test_a_gemini_role_requires_an_api_key(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("KOMORA_GEMINI_API_KEY", "")
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        message = str(excinfo.value)
        assert "llm_agent" in message, "the error must name which model needs the key"
        assert "gemini_api_key" in message

    def test_all_ollama_config_boots_without_a_gemini_key(self, env: pytest.MonkeyPatch) -> None:
        """The local dev profile must work with no API key at all."""
        env.delenv("KOMORA_GEMINI_API_KEY", raising=False)
        env.setenv("KOMORA_LLM_AGENT", "ollama/gemma4:12b")
        env.setenv("KOMORA_LLM_VERIFIER", "ollama/gemma4:12b")
        s = Settings(_env_file=None)
        assert s.gemini_api_key == ""
        assert s.model_for("agent") == ("ollama", "gemma4:12b")

    def test_mixed_config_still_requires_the_key(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("KOMORA_GEMINI_API_KEY", "")
        env.setenv("KOMORA_LLM_AGENT", "ollama/gemma4:12b")
        with pytest.raises(ValidationError, match="llm_verifier"):
            Settings(_env_file=None)


class TestRetiredNames:
    """A renamed variable left in a .env is invisible to pydantic-settings, so the app
    would boot on defaults while the operator believed their config was in effect."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [("KOMORA_LLM_LITE", "KOMORA_LLM_AGENT"), ("KOMORA_LLM_FULL", "KOMORA_LLM_VERIFIER")],
    )
    def test_an_old_name_refuses_to_start(
        self, env: pytest.MonkeyPatch, old: str, new: str
    ) -> None:
        env.setenv(old, "gemini/gemini-3.1-flash-lite")
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)
        message = str(excinfo.value)
        assert old in message and new in message, "the error must name the rename"

    def test_an_empty_old_name_is_ignored(self, env: pytest.MonkeyPatch) -> None:
        """An exported-but-blank variable is not a configuration anyone is relying on."""
        env.setenv("KOMORA_LLM_LITE", "")
        assert Settings(_env_file=None).llm_agent == "gemini/gemini-3.5-flash-lite"


def test_model_for_returns_parsed_provider_and_model(env: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    assert s.model_for("agent") == ("gemini", "gemini-3.5-flash-lite")
    assert s.model_for("verifier") == ("gemini", "gemini-3.1-flash-lite")


class TestTheMiniAppLink:
    """It cannot be derived — BotFather chooses the short name and no API reports it —
    so the only two honest states are "configured correctly" and "absent"."""

    def test_absent_by_default(self, env: pytest.MonkeyPatch) -> None:
        assert Settings(_env_file=None).telegram_mini_app_url == ""

    def test_a_t_me_link_is_kept_without_its_trailing_slash(self, env: pytest.MonkeyPatch) -> None:
        env.setenv("KOMORA_TELEGRAM_MINI_APP_URL", "https://t.me/bot/komora/")
        assert Settings(_env_file=None).telegram_mini_app_url == "https://t.me/bot/komora"

    def test_anything_else_refuses_to_start(self, env: pytest.MonkeyPatch) -> None:
        """A button pointing somewhere else is worse than no button: it opens in
        Telegram and blames Komora."""
        env.setenv("KOMORA_TELEGRAM_MINI_APP_URL", "https://komora.example/app")
        with pytest.raises(ValidationError, match=re.escape("t.me")):
            Settings(_env_file=None)
