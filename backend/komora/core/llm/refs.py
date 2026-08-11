"""Model references.

A model is named by a `provider/model` string so that switching model — or provider —
is a config change rather than a code change. See the design spec §4.1.
"""

from typing import Final, Literal, get_args

Provider = Literal["gemini", "ollama"]
Tier = Literal["lite", "full"]

KNOWN_PROVIDERS: Final[tuple[Provider, ...]] = get_args(Provider)


def parse_model_ref(ref: str) -> tuple[Provider, str]:
    """Split a `provider/model` reference into its parts.

    Splits on the *first* slash only. Ollama tags embed colons (`ollama/gemma4:12b`)
    and model names may themselves contain slashes (`ollama/hf.co/user/model:q4`),
    so anything after the first separator is the model name verbatim.

    Raises:
        ValueError: the ref is malformed or names a provider we have no client for.
    """
    provider, separator, model = ref.strip().partition("/")
    provider, model = provider.strip(), model.strip()

    if not separator or not provider or not model:
        raise ValueError(
            f"Model ref must be 'provider/model', got {ref!r}. "
            f"Known providers: {', '.join(KNOWN_PROVIDERS)}"
        )

    if provider not in KNOWN_PROVIDERS:
        raise ValueError(
            f"Unknown LLM provider {provider!r} in ref {ref!r}. "
            f"Known providers: {', '.join(KNOWN_PROVIDERS)}"
        )

    # mypy narrows `provider` to Provider from the membership check above.
    return provider, model
