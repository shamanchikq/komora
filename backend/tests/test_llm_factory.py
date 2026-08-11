"""make_llm resolves a `provider/model` ref to a client."""

import pytest

from komora.config import Settings
from komora.core.llm.factory import make_llm
from komora.core.llm.gemini.client import GeminiClient
from komora.core.llm.ollama.client import OllamaClient
from komora.core.llm.protocol import LLMClient

BASE = {
    "KOMORA_TELEGRAM_BOT_TOKEN": "1:A",
    "KOMORA_TOKEN_ENCRYPTION_KEY": "k",
    "KOMORA_PUBLIC_BASE_URL": "https://x.example",
    "KOMORA_GEMINI_API_KEY": "gem",
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


@pytest.mark.parametrize("ref", ["gemini/gemini-3.6-flash", "ollama/gemma4:12b"])
def test_both_clients_satisfy_the_protocol(settings: Settings, ref: str) -> None:
    assert isinstance(make_llm(ref, settings), LLMClient)


def test_unknown_provider_is_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="openai"):
        make_llm("openai/gpt-5", settings)


def test_malformed_ref_is_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        make_llm("gemini-3.6-flash", settings)


def test_tier_refs_from_settings_resolve(settings: Settings) -> None:
    """The path the agent actually uses."""
    for tier in ("lite", "full"):
        provider, model = settings.tier_ref(tier)  # type: ignore[arg-type]
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
