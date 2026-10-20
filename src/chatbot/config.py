"""Settings loaded from environment / `.env`. Every path and limit lives here."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

Provider = Literal["anthropic", "meta", "local"]
TokenCountMode = Literal["auto", "server", "estimate"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    llm_provider: Provider = "anthropic"
    llm_model: str = "claude-sonnet-5-5"

    anthropic_api_key: SecretStr | None = None
    meta_api_key: SecretStr | None = None
    local_base_url: str = "http://localhost:11434/v1"
    local_api_key: SecretStr = SecretStr("local")

    # Hard cap on input tokens (incl. cached) for every single model request.
    max_input_tokens: int = 8000
    # How to size a request before sending it:
    #   server   = provider token-count endpoint (falls back to estimate if unsupported)
    #   estimate = local character-based estimate
    #   auto     = server for anthropic, estimate otherwise
    token_count_mode: TokenCountMode = "auto"
    # Conservative chars-per-token ratio for the local estimate (lower = safer).
    estimate_chars_per_token: float = 3.0
    # Raise when the provider-reported input exceeds the cap (otherwise only logged).
    budget_strict: bool = True

    # Max estimated tokens of verbatim recent turns replayed into each chat request.
    history_token_budget: int = 2500
    # The replayed window starts on a multiple of this many turns, so it stays put (and the
    # prompt prefix stays cacheable) for several turns before sliding forward.
    history_window_step: int = 8

    default_user_id: str = "me"
    state_dir: Path = Path(".state")

    @property
    def state_path(self) -> Path:
        path = self.state_dir if self.state_dir.is_absolute() else REPO_ROOT / self.state_dir
        return path

    @property
    def db_path(self) -> Path:
        return self.state_path / "state.db"

    @property
    def llm_log_path(self) -> Path:
        return self.state_path / "logs" / "llm_calls.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()
