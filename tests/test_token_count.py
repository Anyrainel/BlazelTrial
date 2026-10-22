"""Local count uses inference serialization, with no real network."""

import asyncio
import json

import httpx
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from chatbot.agent import token_count


def test_local_count_renders_the_inference_messages(monkeypatch):
    payloads = []

    def handle(request):
        payloads.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "rendered prompt"})
        return httpx.Response(200, json={"tokens": [1, 2, 3]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        token_count.httpx,
        "AsyncClient",
        lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle)),
    )
    model = OpenAIChatModel(
        "test", provider=OpenAIProvider(base_url="http://local/v1", api_key="fake")
    )
    messages = [ModelRequest(parts=[UserPromptPart("hello")], instructions="system")]
    params = ModelRequestParameters()

    async def check():
        settings, prepared = model.prepare_request(None, params)
        expected = await model._map_messages(messages, prepared, model_settings=settings)
        n = await token_count.local_counter("http://local/v1", model)(messages, params, None)
        assert n == 3
        assert payloads[0][1]["messages"] == expected
        assert payloads[1] == ("/tokenize", {"content": "rendered prompt"})

    asyncio.run(check())
