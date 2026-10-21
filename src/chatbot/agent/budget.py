"""Per-request input-token budget and call log.

Every model request goes through `BudgetedModel`:
  1. pre-flight: size the request (provider count endpoint or local estimate) and refuse it
     before sending if it is over `max_input_tokens`;
  2. send;
  3. log one JSON line with the provider-reported usage, and fail if it exceeded the cap.

PydanticAI normalises `RequestUsage.input_tokens` to include cache reads/writes, so that single
field is the number we enforce.
"""

import json
import logging
import threading
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

logger = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    def __init__(self, tokens: int, limit: int, stage: str):
        super().__init__(f"{stage}: {tokens} input tokens exceeds budget of {limit}")
        self.tokens = tokens
        self.limit = limit
        self.stage = stage


@dataclass(frozen=True)
class CallContext:
    thread_id: str | None = None
    purpose: str = "chat"


_call_context: ContextVar[CallContext] = ContextVar(
    "call_context",
    default=CallContext(),  # noqa: B039 (frozen dataclass, immutable)
)


@contextmanager
def call_context(*, thread_id: str | None = None, purpose: str = "chat") -> Iterator[None]:
    """Tag every model request made inside this block (shows up in the call log).

    Without `thread_id`, the enclosing block's thread is kept (e.g. a tool's own model calls).
    """
    thread_id = thread_id if thread_id is not None else _call_context.get().thread_id
    token = _call_context.set(CallContext(thread_id=thread_id, purpose=purpose))
    try:
        yield
    finally:
        _call_context.reset(token)


class CallLog:
    """Append-only JSONL log, one line per model request."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# Per-part allowance for role markers, tool-call framing, etc.
_PART_OVERHEAD_TOKENS = 8


def _part_text(part: Any) -> str:
    if hasattr(part, "model_response_str"):  # tool return / retry prompt
        return part.model_response_str()
    if hasattr(part, "args_as_json_str"):  # tool call
        return part.tool_name + part.args_as_json_str()
    content = getattr(part, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # user content: text items count, markers (CachePoint) don't
        return " ".join(c if isinstance(c, str) else "" for c in content)
    return json.dumps(content, default=str)


def estimate_tokens(
    messages: list[ModelMessage], params: ModelRequestParameters, chars_per_token: float
) -> int:
    """Conservative local estimate of what the provider will count.

    Counts the content actually sent (text, tool args/returns, instructions, tool schemas), not
    PydanticAI's stored metadata, at a pessimistic chars-per-token ratio plus per-part overhead.
    Only the most recent instructions count: providers are sent those, not each request's copy.
    """
    latest = next(
        (m.instructions for m in reversed(messages) if getattr(m, "instructions", None)), None
    )
    chars, parts = len(latest or ""), 0
    for message in messages:
        for part in message.parts:
            chars += len(_part_text(part))
            parts += 1
    tools = [*params.function_tools, *params.output_tools]
    chars += len(
        json.dumps(
            [{"n": t.name, "d": t.description, "p": t.parameters_json_schema} for t in tools]
        )
    )
    return int(chars / chars_per_token) + parts * _PART_OVERHEAD_TOKENS + 1


class BudgetedModel(WrapperModel):
    def __init__(
        self,
        wrapped: Model,
        *,
        max_input_tokens: int,
        use_server_count: bool,
        chars_per_token: float,
        safety_factor: float = 1.0,
        strict: bool,
        log: CallLog,
    ):
        super().__init__(wrapped)
        self.max_input_tokens = max_input_tokens
        self.use_server_count = use_server_count
        self.chars_per_token = chars_per_token
        self.safety_factor = safety_factor
        self.strict = strict
        self.log = log

    async def preflight(
        self,
        messages: list[ModelMessage],
        settings: ModelSettings | None,
        params: ModelRequestParameters,
    ) -> tuple[int, str]:
        """Return (estimated input tokens, method). Raises before sending if over budget."""
        tokens, method = None, "estimate"
        if self.use_server_count:
            try:
                tokens = (await self.wrapped.count_tokens(messages, settings, params)).input_tokens
                method = "server"
            except Exception as exc:  # unsupported endpoint, network error, ...
                logger.warning("server token count failed, falling back to estimate: %s", exc)
        if tokens is None:
            raw = estimate_tokens(messages, params, self.chars_per_token)
            tokens = int(raw * self.safety_factor)
        if tokens > self.max_input_tokens:
            self._log(None, tokens, method, error="preflight_over_budget")
            raise BudgetExceededError(tokens, self.max_input_tokens, "preflight")
        return tokens, method

    def _log(
        self,
        usage: RequestUsage | None,
        preflight: int,
        method: str,
        error: str | None = None,
    ) -> None:
        ctx = _call_context.get()
        self.log.write(
            {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "thread_id": ctx.thread_id,
                "purpose": ctx.purpose,
                "model": self.model_name,
                "limit": self.max_input_tokens,
                "preflight_tokens": preflight,
                "preflight_method": method,
                "input_tokens": usage.input_tokens if usage else None,
                "cache_read_tokens": usage.cache_read_tokens if usage else None,
                "cache_write_tokens": usage.cache_write_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "within_budget": usage.input_tokens <= self.max_input_tokens if usage else False,
                "error": error,
            }
        )

    def _check_reported(self, usage: RequestUsage, preflight: int, method: str) -> None:
        over = usage.input_tokens > self.max_input_tokens
        self._log(usage, preflight, method, error="reported_over_budget" if over else None)
        if over and self.strict:
            raise BudgetExceededError(usage.input_tokens, self.max_input_tokens, "reported")

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        preflight, method = await self.preflight(messages, model_settings, model_request_parameters)
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        self._check_reported(response.usage, preflight, method)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncGenerator[StreamedResponse]:
        preflight, method = await self.preflight(messages, model_settings, model_request_parameters)
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream
        self._check_reported(stream.usage(), preflight, method)
