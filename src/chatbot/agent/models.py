"""Model factory. The only place a provider model is constructed."""

import logging

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.meta import MetaProvider
from pydantic_ai.providers.openai import OpenAIProvider

from chatbot.agent import local_server
from chatbot.agent.budget import BudgetedModel, CallLog
from chatbot.config import Settings

logger = logging.getLogger(__name__)

# Per-process verdict on the local server, keyed by LOCAL_BASE_URL: None = up, else the reason
# it is unavailable. Checked once, so the agents built after the first don't each wait again.
_local_unavailable: dict[str, str | None] = {}


def _secret(value, name: str) -> str:
    if value is None or not value.get_secret_value():
        raise RuntimeError(f"{name} is not set; add it to .env")
    return value.get_secret_value()


def build_raw_model(settings: Settings) -> Model:
    match settings.llm_provider:
        case "anthropic":
            provider = AnthropicProvider(
                api_key=_secret(settings.anthropic_api_key, "ANTHROPIC_API_KEY")
            )
            # Automatic prompt caching: stable prefixes (instructions, older turns) get cache hits.
            cache = AnthropicModelSettings(anthropic_cache=True)
            return AnthropicModel(settings.llm_model, provider=provider, settings=cache)
        case "meta":
            provider = MetaProvider(api_key=_secret(settings.meta_api_key, "META_API_KEY"))
            return OpenAIResponsesModel(settings.llm_model, provider=provider)
        case "local":
            provider = OpenAIProvider(
                base_url=settings.local_base_url, api_key=settings.local_api_key.get_secret_value()
            )
            return OpenAIChatModel(settings.llm_model, provider=provider)


def _local_problem(settings: Settings) -> str | None:
    """Why the local server can't be used (None if it is up), starting it if allowed."""
    key = settings.local_base_url
    if key not in _local_unavailable:
        try:
            if settings.local_autostart:
                local_server.ensure_running(settings)
            elif not local_server.status(settings).running:
                raise RuntimeError(f"nothing answers on {key} and LOCAL_AUTOSTART is off")
            _local_unavailable[key] = None
        except (RuntimeError, OSError) as exc:  # TimeoutError is an OSError
            _local_unavailable[key] = str(exc)
            logger.warning(
                "local model unavailable (%s); using fallback %s:%s",
                exc,
                settings.llm_fallback_provider,
                settings.llm_fallback_model,
            )
    return _local_unavailable[key]


def effective_settings(settings: Settings) -> Settings:
    """`settings`, or a copy on LLM_FALLBACK_PROVIDER/MODEL when the local model is unavailable."""
    if settings.llm_provider != "local":
        return settings
    problem = _local_problem(settings)
    if problem is None:
        return settings
    if settings.llm_fallback_provider == "none":
        raise RuntimeError(f"local model unavailable and LLM_FALLBACK_PROVIDER=none: {problem}")
    return settings.model_copy(
        update={
            "llm_provider": settings.llm_fallback_provider,
            "llm_model": settings.llm_fallback_model,
        }
    )


def build_model(settings: Settings, raw: Model | None = None) -> BudgetedModel:
    """Wrap the provider model with budget guard + call log. `raw` injects a test model."""
    if raw is None:
        settings = effective_settings(settings)
    return BudgetedModel(
        raw or build_raw_model(settings),
        max_input_tokens=settings.max_input_tokens,
        use_server_count=settings.uses_server_count,
        chars_per_token=settings.estimate_chars_per_token,
        safety_factor=settings.estimate_safety_factor,
        strict=settings.budget_strict,
        log=CallLog(settings.llm_log_path),
    )
