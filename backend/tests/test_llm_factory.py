"""make_llm resolves a `provider/model` ref to a client."""

import pytest

from komora.config import Settings
from komora.core.llm.factory import make_llm
from komora.core.llm.gemini.client import GeminiClient
from komora.core.llm.ollama.client import OllamaClient
from komora.core.llm.openrouter.client import OpenRouterClient
from komora.core.llm.protocol import LLMClient

BASE = {
    "KOMORA_TELEGRAM_BOT_TOKEN": "1:A",
    "KOMORA_TOKEN_ENCRYPTION_KEY": "k",
    "KOMORA_PUBLIC_BASE_URL": "https://x.example",
    "KOMORA_GEMINI_API_KEY": "gem",
    "KOMORA_OPENROUTER_API_KEY": "or",
}


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_gemini_ref_builds_a_gemini_client(settings: Settings) -> None:
    client = make_llm("gemini/gemini-3.1-flash-lite", settings)
    assert isinstance(client, GeminiClient)
    assert client.model == "gemini-3.1-flash-lite"


def test_ollama_ref_builds_an_ollama_client(settings: Settings) -> None:
    """The tag contains a colon, which must survive the ref split."""
    client = make_llm("ollama/gemma4:12b", settings)
    assert isinstance(client, OllamaClient)
    assert client.model == "gemma4:12b"


def test_openrouter_ref_keeps_the_slash_in_the_model_id(settings: Settings) -> None:
    """Every OpenRouter id has a slash of its own, so the ref has two: the split takes
    the first and the rest is the model name verbatim."""
    client = make_llm("openrouter/stealth/ox-alpha", settings)
    assert isinstance(client, OpenRouterClient)
    assert client.model == "stealth/ox-alpha"


@pytest.mark.parametrize(
    "ref", ["gemini/gemini-3.6-flash", "ollama/gemma4:12b", "openrouter/stealth/ox-alpha"]
)
def test_every_client_satisfies_the_protocol(settings: Settings, ref: str) -> None:
    assert isinstance(make_llm(ref, settings), LLMClient)


def test_unknown_provider_is_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="openai"):
        make_llm("openai/gpt-5", settings)


def test_malformed_ref_is_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        make_llm("gemini-3.6-flash", settings)


def test_role_refs_from_settings_resolve(settings: Settings) -> None:
    """The path the agent actually uses."""
    for role in ("agent", "verifier"):
        provider, model = settings.model_for(role)  # type: ignore[arg-type]
        assert isinstance(make_llm(f"{provider}/{model}", settings), LLMClient)


def test_ollama_client_takes_its_base_url_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KOMORA_OLLAMA_BASE_URL", "http://gpu-box:11434/")
    client = make_llm("ollama/gemma4:12b", Settings(_env_file=None))
    assert isinstance(client, OllamaClient)
    assert client.base_url == "http://gpu-box:11434", "trailing slash must be stripped"


def test_an_openrouter_ref_without_a_key_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule Gemini has had: fail at startup, not on the first basket."""
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("KOMORA_OPENROUTER_API_KEY")
    monkeypatch.setenv("KOMORA_LLM_AGENT", "openrouter/stealth/ox-alpha")
    with pytest.raises(ValueError, match="KOMORA_OPENROUTER_API_KEY"):
        Settings(_env_file=None)
