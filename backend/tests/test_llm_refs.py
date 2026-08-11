"""Model refs are `provider/model` strings, so switching provider is config not code."""

import pytest

from komora.core.llm.refs import KNOWN_PROVIDERS, parse_model_ref


def test_parses_gemini_ref() -> None:
    assert parse_model_ref("gemini/gemini-3.1-flash-lite") == ("gemini", "gemini-3.1-flash-lite")


def test_parses_ollama_tag_containing_a_colon() -> None:
    """Ollama tags embed a colon — the ref must survive it intact."""
    assert parse_model_ref("ollama/gemma4:12b") == ("ollama", "gemma4:12b")


def test_splits_on_first_slash_only() -> None:
    """A model name may itself contain slashes (e.g. HuggingFace-style org/name)."""
    assert parse_model_ref("ollama/hf.co/user/model:q4") == ("ollama", "hf.co/user/model:q4")


@pytest.mark.parametrize("ref", ["gemini-3.6-flash", "", "   "])
def test_ref_without_provider_is_rejected(ref: str) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        parse_model_ref(ref)


def test_unknown_provider_is_rejected_and_lists_the_known_ones() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_model_ref("openai/gpt-5")
    message = str(excinfo.value)
    assert "openai" in message
    for provider in KNOWN_PROVIDERS:
        assert provider in message, "the error must tell you what you *can* use"


def test_empty_model_part_is_rejected() -> None:
    with pytest.raises(ValueError, match="provider/model"):
        parse_model_ref("gemini/")


def test_surrounding_whitespace_is_tolerated() -> None:
    """Values arriving from .env files routinely carry stray spaces."""
    assert parse_model_ref("  gemini/gemini-3.6-flash  ") == ("gemini", "gemini-3.6-flash")
