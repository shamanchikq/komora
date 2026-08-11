"""Application settings, read from the environment (prefix `KOMORA_`) or a .env file."""

from typing import Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from komora.core.llm.refs import Provider, Tier, parse_model_ref

_TIER_FIELDS: dict[Tier, str] = {"lite": "llm_lite", "full": "llm_full"}


class Settings(BaseSettings):
    """Config for the whole app. See `.env.example` for documented values."""

    # --- Required ---
    telegram_bot_token: str
    token_encryption_key: str
    """urlsafe-base64 of 32 random bytes. Rotating it invalidates every stored token."""
    public_base_url: str
    """Public HTTPS base for the Silpo OAuth callback. A tunnel URL in development."""

    # Required only when a tier uses a gemini/* model — an all-Ollama config needs no key.
    gemini_api_key: str = ""

    # --- Optional ---
    silpo_mcp_url: str = "https://mcp.silpo.ua/mcp"
    database_url: str = "sqlite+aiosqlite:///./komora.db"
    http_port: int = 8000

    # Two-tier model routing as `provider/model` refs; see core.llm.refs.
    llm_lite: str = "gemini/gemini-3.1-flash-lite"
    llm_full: str = "gemini/gemini-3.6-flash"
    ollama_base_url: str = "http://localhost:11434"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="KOMORA_")

    @field_validator("public_base_url", "ollama_base_url", "silpo_mcp_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """URLs are joined by concatenation; a trailing slash yields a broken `//` path."""
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _validate_model_refs(self) -> Self:
        """Fail at startup rather than on the first LLM call."""
        for tier, field in _TIER_FIELDS.items():
            ref: str = getattr(self, field)
            provider, _ = parse_model_ref(ref)  # rejects unknown providers
            if provider == "gemini" and not self.gemini_api_key.strip():
                raise ValueError(
                    f"{field}={ref!r} uses the gemini provider, so gemini_api_key must be set"
                    f" (KOMORA_GEMINI_API_KEY). Point the {tier!r} tier at an ollama/* model"
                    f" to run without an API key."
                )
        return self

    def tier_ref(self, tier: Tier) -> tuple[Provider, str]:
        """Resolve a tier to its `(provider, model)` pair."""
        return parse_model_ref(getattr(self, _TIER_FIELDS[tier]))
