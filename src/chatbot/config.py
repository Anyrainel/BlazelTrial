"""Settings loaded from environment / `.env`. Every path and limit lives here."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

Provider = Literal["anthropic", "meta", "local"]
TokenCountMode = Literal["auto", "server", "tokenize", "estimate"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    llm_provider: Provider = "local"
    llm_model: str = "bonsai-2-27b"
    # Used instead when LLM_PROVIDER=local but the local server can't be reached or started
    # (decided once per process). "none" = fail instead.
    llm_fallback_provider: Provider | Literal["none"] = "anthropic"
    llm_fallback_model: str = "claude-sonnet-5-5"

    anthropic_api_key: SecretStr | None = None
    meta_api_key: SecretStr | None = None
    local_base_url: str = "http://localhost:11434/v1"
    local_api_key: SecretStr = SecretStr("local")
    # llama.cpp server for LLM_PROVIDER=local, started on first use if nothing answers on
    # LOCAL_BASE_URL (`chatbot local-server` starts/stops it by hand). Bonsai 2 needs
    # PrismML's llama.cpp fork; LLM_MODEL becomes the served model name.
    local_autostart: bool = True
    local_server_bin: Path | None = None
    local_model_path: Path | None = None
    local_context: int = 65536  # total KV context, split across the parallel slots
    local_parallel: int = 2
    local_reasoning: Literal["on", "off", "auto"] = "auto"
    local_start_timeout: float = 180.0

    # Hard cap on input tokens (incl. cached) for every single model request.
    max_input_tokens: int = 8000
    # How to size a request before sending it:
    #   server   = provider token-count endpoint (falls back to estimate if unsupported)
    #   tokenize = the local llama.cpp server renders its chat template and tokenizes (exact)
    #   estimate = local character-based estimate
    #   auto     = server for anthropic and meta, tokenize for local (each falls back to estimate)
    token_count_mode: TokenCountMode = "auto"
    # Conservative chars-per-token ratio for the local estimate (lower = safer).
    estimate_chars_per_token: float = 3.0
    # Multiplier on the local estimate. Measured on Meta: provider count was up to 1.31x our raw
    # estimate (hidden structured-output/tool framing), so 1.5 keeps the pre-flight conservative.
    estimate_safety_factor: float = 1.5
    # Safety margin on the local tokenizer's count (template framing of replayed tool calls is
    # approximated). Any count that comes in low raises its scale automatically.
    tokenizer_margin: float = 1.05
    # Raise when the provider-reported input exceeds the cap. Off by default: a response that
    # already came back is more useful than an error, and the overshoot is logged and learned from.
    budget_strict: bool = False

    # Max estimated tokens of verbatim recent turns replayed into each chat request.
    history_token_budget: int = 2500
    # The replayed window starts on a multiple of this many turns, so it stays put (and the
    # prompt prefix stays cacheable) for several turns before sliding forward.
    history_window_step: int = 8

    # Embeddings (sqlite-vec): optional vector candidates for memory search and doc passages.
    embedding_backend: Literal["fastembed", "hash"] = "fastembed"  # hash = offline/tests
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Cosine distance cut-off for vector hits (bge-small: unrelated text sits around 0.4-0.6).
    retrieval_max_distance: float = 0.35

    # Chat context is assembled to at most this share of MAX_INPUT_TOKENS (estimate), leaving
    # headroom for estimate error; plus a margin for request framing (tool schemas and
    # capability instructions are measured, not guessed).
    context_fill_ratio: float = 0.8
    tool_overhead_tokens: int = 150

    # Documents (fetch_doc): read from DOCS_DIR (default: STATE_DIR/docs).
    docs_dir: Path | None = None
    doc_full_text_tokens: int = 1500  # smaller docs are returned whole
    doc_chunk_tokens: int = 2500  # map step input size
    doc_passage_chars: int = 1200  # search unit for search_doc
    doc_search_token_budget: int = 1500
    doc_note_concurrency: int = 6

    # ask_memory subagent bounds: model requests per question, total tool-output characters
    # it may read, and the answer size handed back to the main agent.
    memory_search_max_requests: int = 6
    memory_search_tool_chars: int = 12000
    memory_answer_chars: int = 1500
    # Write and forget tasks of the same worker, and the most messages one string may erase
    # without the worker confirming that the user meant everything.
    memory_write_max_requests: int = 6
    memory_forget_max_requests: int = 8
    memory_erase_max_messages: int = 15

    # Episode notes: fixed segments of this many turns, written once they are this far behind
    # the newest turn (searchable via ask_memory, never injected).
    summary_chunk_turns: int = 25
    summary_keep_recent_turns: int = 16

    # Slack (Socket Mode). Every DM maps to DEFAULT_USER_ID; set SLACK_ALLOWED_USER (a Slack
    # user id like U0123) to ignore DMs from anyone else.
    slack_bot_token: SecretStr | None = None
    slack_app_token: SecretStr | None = None
    slack_allowed_user: str | None = None

    # HTTP surface
    http_host: str = "127.0.0.1"
    http_port: int = 8000

    default_user_id: str = "me"
    state_dir: Path = Path(".state")

    @property
    def state_path(self) -> Path:
        path = self.state_dir if self.state_dir.is_absolute() else REPO_ROOT / self.state_dir
        return path

    @property
    def count_method(self) -> Literal["server", "tokenize", "estimate"]:
        """How pre-flight sizes requests for the configured provider."""
        if self.token_count_mode != "auto":
            return self.token_count_mode
        return {"anthropic": "server", "meta": "server", "local": "tokenize"}[self.llm_provider]

    @property
    def uses_server_count(self) -> bool:
        """Whether pre-flight sizes requests with the provider's count endpoint."""
        return self.count_method == "server"

    @property
    def context_target_tokens(self) -> int:
        """Estimated-token target for assembling a request. Exact counts (provider or tokenizer)
        shrink oversized requests anyway; only a pure local estimate (x safety factor) aims lower
        by the same factor so assembled requests pass."""
        target = self.max_input_tokens * self.context_fill_ratio
        if self.count_method == "estimate":
            target /= self.estimate_safety_factor
        return int(target)

    @property
    def db_path(self) -> Path:
        return self.state_path / "state.db"

    @property
    def docs_path(self) -> Path:
        if self.docs_dir is None:
            return self.state_path / "docs"
        return self.docs_dir if self.docs_dir.is_absolute() else REPO_ROOT / self.docs_dir

    @property
    def models_path(self) -> Path:
        return self.state_path / "models"

    @property
    def llm_log_path(self) -> Path:
        return self.state_path / "logs" / "llm_calls.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()
