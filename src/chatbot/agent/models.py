"""Model factory. The only place a provider model is constructed."""

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.meta import MetaProvider
from pydantic_ai.providers.openai import OpenAIProvider

from chatbot.agent.budget import BudgetedModel, CallLog
from chatbot.config import Settings


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


def build_model(settings: Settings, raw: Model | None = None) -> BudgetedModel:
    """Wrap the provider model with budget guard + call log. `raw` injects a test model."""
    use_server = settings.token_count_mode == "server" or (
        settings.token_count_mode == "auto" and settings.llm_provider in ("anthropic", "meta")
    )
    return BudgetedModel(
        raw or build_raw_model(settings),
        max_input_tokens=settings.max_input_tokens,
        use_server_count=use_server,
        chars_per_token=settings.estimate_chars_per_token,
        safety_factor=settings.estimate_safety_factor,
        strict=settings.budget_strict,
        log=CallLog(settings.llm_log_path),
    )
