"""Count local inference input using the same pinned adapter serializers as inference."""

from collections.abc import Awaitable, Callable

import httpx
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.settings import ModelSettings

Counter = Callable[
    [list[ModelMessage], ModelRequestParameters, ModelSettings | None], Awaitable[int]
]


def local_counter(base_url: str, model: OpenAIChatModel, timeout: float = 10.0) -> Counter:
    """Render and tokenize with the server that will execute this request.

    Private serialization helpers match the locked PydanticAI version; verify the
    counter alongside inference when upgrading that dependency.
    """
    root = base_url.rstrip("/").removesuffix("/v1")

    async def count(
        messages: list[ModelMessage],
        params: ModelRequestParameters,
        settings: ModelSettings | None,
    ) -> int:
        prepared_settings, prepared_params = model.prepare_request(settings, params)
        prepared_settings = prepared_settings or {}
        chat = await model._map_messages(
            messages, prepared_params, model_settings=prepared_settings
        )
        tools, _ = model._get_tool_choice(prepared_settings, prepared_params)
        model._finalize_cache_breakpoints(tools, chat)
        body: dict = {"messages": chat}
        if tools:
            body["tools"] = tools
        async with httpx.AsyncClient(base_url=root, timeout=timeout) as client:
            rendered = await client.post("/apply-template", json=body)
            rendered.raise_for_status()
            tokens = await client.post("/tokenize", json={"content": rendered.json()["prompt"]})
            tokens.raise_for_status()
            return len(tokens.json()["tokens"])

    return count
