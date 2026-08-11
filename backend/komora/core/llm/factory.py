"""Resolve a `provider/model` reference to a client."""

from komora.config import Settings
from komora.core.llm.gemini.client import GeminiClient
from komora.core.llm.ollama.client import OllamaClient
from komora.core.llm.protocol import LLMClient
from komora.core.llm.refs import parse_model_ref


def make_llm(ref: str, settings: Settings) -> LLMClient:
    """Build the client for `ref`, e.g. "gemini/gemini-3.6-flash" or "ollama/gemma4:12b".

    Raises:
        ValueError: the ref is malformed or names an unknown provider.
    """
    provider, model = parse_model_ref(ref)
    if provider == "gemini":
        return GeminiClient(model=model, api_key=settings.gemini_api_key)
    return OllamaClient(model=model, base_url=settings.ollama_base_url)
