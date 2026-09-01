"""Application settings, read from the environment (prefix `KOMORA_`) or a .env file."""

import os
from typing import Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from komora.core.llm.refs import Provider, Role, parse_model_ref

_ROLE_FIELDS: dict[Role, str] = {"agent": "llm_agent", "verifier": "llm_verifier"}

_RETIRED_VARS: dict[str, str] = {
    "KOMORA_LLM_LITE": "KOMORA_LLM_AGENT",
    "KOMORA_LLM_FULL": "KOMORA_LLM_VERIFIER",
}
"""Renamed 2026-08-12. A stale name in a .env would otherwise be read as no setting at
all, and the app would start happily on defaults — the same silent-config failure that
let a dead `llm_full` sit unnoticed for weeks. Better to refuse to start."""


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
    openrouter_api_key: str = ""
    """Required only when a tier uses an openrouter/* model.

    Worth having as a second hosted option because the constraint Komora actually hits
    is Gemini's free-tier **requests** per day, keyed on (project, model). Pointing one
    tier at OpenRouter draws that tier from an allowance Google does not count."""

    # --- Optional ---
    telegram_mini_app_url: str = ""
    """The Mini App's own `t.me` link, exactly as BotFather gives it — e.g.
    `https://t.me/moya_komora_bot/komora`. Deep links are built by appending
    `?startapp=…` to it.

    Empty by default and empty is a working configuration: the bot simply renders no
    «Відкрити в Коморі» button. It cannot be derived — the short name is chosen in
    BotFather and the API does not report it — and a guessed link would open a 404
    inside Telegram, which reads as Komora being broken."""

    silpo_mcp_url: str = "https://mcp.silpo.ua/mcp"
    database_url: str = "sqlite+aiosqlite:///./komora.db"
    http_port: int = 8000

    # One model per job, as `provider/model` refs; see core.llm.refs.
    #
    # They default to two DIFFERENT models on purpose: free-tier quota is keyed on
    # (project, model) and a basket spends one request on each, so two models means two
    # daily allowances and one model means half the baskets. Which model does which job
    # is measured, not ranked (scripts/compare_models.py, n=5 twice): 3.5-flash-lite
    # names a Silpo category on essentially every basket line where 3.1 manages roughly
    # half, and 3.1 always returns a usable re-search query for a flagged line where 3.5
    # leaves it empty two times in five.
    llm_agent: str = "gemini/gemini-3.5-flash-lite"
    """Runs the agent loop and writes the basket. Wants reliable category naming."""
    llm_verifier: str = "gemini/gemini-3.1-flash-lite"
    """Runs the verification pass. Wants a usable `better_query` on every flag — a model
    that returns an empty one makes `pipeline._verified` drop the line instead of
    repairing it. Deliberately the cheaper model; this is not a downgrade."""
    ollama_base_url: str = "http://localhost:11434"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="KOMORA_")

    @field_validator("telegram_mini_app_url")
    @classmethod
    def _require_a_telegram_link(cls, value: str) -> str:
        """Refuse anything that is not a `t.me` link rather than render a dead button."""
        link = value.strip().rstrip("/")
        if link and not link.startswith("https://t.me/"):
            raise ValueError(
                "KOMORA_TELEGRAM_MINI_APP_URL must be the Mini App's t.me link "
                "(https://t.me/<bot>/<app>), or empty to render no deep link."
            )
        return link

    @field_validator("public_base_url", "ollama_base_url", "silpo_mcp_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """URLs are joined by concatenation; a trailing slash yields a broken `//` path."""
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _reject_retired_names(self) -> Self:
        """A renamed variable still sitting in a .env must not be silently ignored.

        pydantic-settings simply does not see an unknown key, so the app would start on
        defaults and the operator would believe their configuration was in effect. That
        is how `llm_full` went unnoticed: config that looks set and does nothing.
        """
        stale = [old for old in _RETIRED_VARS if os.environ.get(old)]
        if stale:
            renames = ", ".join(f"{old} -> {_RETIRED_VARS[old]}" for old in stale)
            raise ValueError(
                f"These settings were renamed and are no longer read: {renames}. "
                f"They name a job now, not a size — the verifier is the cheaper model on "
                f"purpose. Update your .env; leaving the old name would silently fall "
                f"back to defaults."
            )
        return self

    @model_validator(mode="after")
    def _validate_model_refs(self) -> Self:
        """Fail at startup rather than on the first LLM call."""
        needs_key: dict[str, tuple[str, str]] = {
            "gemini": (self.gemini_api_key, "KOMORA_GEMINI_API_KEY"),
            "openrouter": (self.openrouter_api_key, "KOMORA_OPENROUTER_API_KEY"),
        }
        for role, field in _ROLE_FIELDS.items():
            ref: str = getattr(self, field)
            provider, _ = parse_model_ref(ref)  # rejects unknown providers
            if provider in needs_key and not needs_key[provider][0].strip():
                raise ValueError(
                    f"{field}={ref!r} uses the {provider} provider, so its API key must be"
                    f" set ({needs_key[provider][1]}). Point the {role!r} model at an"
                    f" ollama/* ref to run without an API key."
                )
        return self

    def model_for(self, role: Role) -> tuple[Provider, str]:
        """Resolve a role to its `(provider, model)` pair."""
        return parse_model_ref(getattr(self, _ROLE_FIELDS[role]))
