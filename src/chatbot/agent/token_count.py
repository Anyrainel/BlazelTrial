"""Exact input-token counts for the local llama.cpp server.

The server renders its own chat template (`/apply-template`) and tokenizes with the loaded
model's vocabulary (`/tokenize`), so template framing, tool definitions and role markers are
counted exactly as a real request would be (checked against the server's `prompt_tokens`).
No count endpoint exists for the OpenAI-compatible route, so this stands in for it.
"""

from collections.abc import Awaitable, Callable

import httpx
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

Counter = Callable[[list[ModelMessage], ModelRequestParameters], Awaitable[int]]


def _user_text(part: UserPromptPart) -> str:
    if isinstance(part.content, str):
        return part.content
    return " ".join(c for c in part.content if isinstance(c, str))  # markers carry no tokens


def chat_payload(
    messages: list[ModelMessage], params: ModelRequestParameters
) -> tuple[list[dict], list[dict]]:
    """The request as OpenAI-style chat messages and tool definitions (approximate framing)."""
    chat: list[dict] = []
    latest = next(
        (m.instructions for m in reversed(messages) if getattr(m, "instructions", None)), None
    )
    if latest:
        chat.append({"role": "system", "content": latest})
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    chat.append({"role": "system", "content": part.content})
                elif isinstance(part, UserPromptPart):
                    chat.append({"role": "user", "content": _user_text(part)})
                elif isinstance(part, ToolReturnPart):
                    chat.append(
                        {
                            "role": "tool",
                            "tool_call_id": part.tool_call_id,
                            "content": part.model_response_str(),
                        }
                    )
                elif isinstance(part, RetryPromptPart):
                    chat.append({"role": "user", "content": part.model_response()})
        elif isinstance(message, ModelResponse):
            text = "".join(p.content for p in message.parts if isinstance(p, TextPart))
            calls = [
                {
                    "id": p.tool_call_id,
                    "type": "function",
                    "function": {"name": p.tool_name, "arguments": p.args_as_json_str()},
                }
                for p in message.parts
                if isinstance(p, ToolCallPart)
            ]
            entry: dict = {"role": "assistant", "content": text}
            if calls:
                entry["tool_calls"] = calls
            chat.append(entry)
    tools = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.parameters_json_schema,
            },
        }
        for t in [*params.function_tools, *params.output_tools]
    ]
    return chat, tools


def local_counter(base_url: str, timeout: float = 10.0) -> Counter:
    """A counter for the llama.cpp server behind `base_url` (the OpenAI `/v1` URL)."""
    root = base_url.rstrip("/").removesuffix("/v1")

    async def count(messages: list[ModelMessage], params: ModelRequestParameters) -> int:
        chat, tools = chat_payload(messages, params)
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
