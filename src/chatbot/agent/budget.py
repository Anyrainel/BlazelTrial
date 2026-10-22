"""Per-request input-token budget and call log.

Every model request goes through `BudgetedModel`:
  1. pre-flight: size the request, most exact first: the provider's count endpoint, the local
     server's own tokenizer, else a pessimistic local estimate;
  2. if it is not below `max_input_tokens`, shrink the copy being sent (agent/shrink.py) and
     re-count, so an oversized request is trimmed instead of failing the user; only a request
     whose fixed part (instructions, tool schemas) cannot fit is refused before sending;
  3. send;
  4. log one JSON line with the provider-reported usage; a count that came in low raises that
     method's safety scale for every later request.

PydanticAI normalises `RequestUsage.input_tokens` to include cache reads/writes, so that single
field is the number we enforce. The budget is exclusive: a request must stay below the limit.
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

from chatbot.agent.shrink import shrink_messages
from chatbot.agent.token_count import Counter

logger = logging.getLogger(__name__)

# Re-count after shrinking at most this many times, aiming a bit under the limit each time.
_MAX_SHRINK_ROUNDS = 3
_SHRINK_HEADROOM = 0.93
_MAX_SCALE = 4.0


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


@dataclass(frozen=True)
class Measure:
    tokens: int  # what the budget is judged on: the raw count x the method's safety scale
    raw: int  # the method's own number
    method: str  # server | tokenizer | estimate


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
        counter: Counter | None = None,
        counter_margin: float = 1.05,
        require_count: bool = False,
    ):
        super().__init__(wrapped)
        self.max_input_tokens = max_input_tokens
        self.use_server_count = use_server_count
        self.chars_per_token = chars_per_token
        self.strict = strict
        self.log = log
        self.counter = counter
        self.require_count = require_count
        # Multiplier per counting method; raised (never lowered) when a provider reports more.
        self.scale = {"server": 1.0, "tokenizer": counter_margin, "estimate": safety_factor}

    def _over(self, tokens: int) -> bool:
        return tokens >= self.max_input_tokens

    async def measure(
        self,
        messages: list[ModelMessage],
        settings: ModelSettings | None,
        params: ModelRequestParameters,
    ) -> Measure:
        """Size a request with the most exact method available (falls back on failure)."""
        raw, method = None, "estimate"
        if self.use_server_count:
            try:
                raw = (await self.wrapped.count_tokens(messages, settings, params)).input_tokens
                method = "server"
            except Exception as exc:  # unsupported endpoint, network error, ...
                logger.warning("server token count failed, falling back: %s", exc)
        if raw is None and self.counter is not None:
            try:
                raw, method = await self.counter(messages, params, settings), "tokenizer"
            except Exception as exc:  # server down, endpoint missing, ...
                logger.warning("tokenizer count failed, falling back to estimate: %s", exc)
        if raw is None:
            if self.require_count:
                raise RuntimeError("Token counting unavailable; inference was not sent.")
            raw = estimate_tokens(messages, params, self.chars_per_token)
        return Measure(int(raw * self.scale[method]) + 1, raw, method)

    async def fit(
        self,
        messages: list[ModelMessage],
        settings: ModelSettings | None,
        params: ModelRequestParameters,
    ) -> tuple[list[ModelMessage], Measure, int | None]:
        """The messages to send, their measure, and the size before shrinking (None if untouched).

        Raises BudgetExceededError("preflight") only when shrinking cannot make it fit.
        """

        def size(ms: list[ModelMessage]) -> int:
            return estimate_tokens(ms, params, self.chars_per_token)

        m = await self.measure(messages, settings, params)
        original = None
        for _ in range(_MAX_SHRINK_ROUNDS):
            if not self._over(m.tokens):
                break
            original = original or m.tokens
            current = size(messages)
            target = int(current * _SHRINK_HEADROOM * self.max_input_tokens / m.tokens)
            shrunk = shrink_messages(messages, size, target)
            if size(shrunk) >= current:
                break  # nothing left that may be cut
            messages = shrunk
            m = await self.measure(messages, settings, params)
        if self._over(m.tokens):
            self._log(None, m, error="preflight_over_budget", shrunk_from=original)
            raise BudgetExceededError(m.tokens, self.max_input_tokens, "preflight")
        if original:
            logger.warning(
                "request shrunk to fit the budget: %d -> %d tokens (%s)",
                original,
                m.tokens,
                m.method,
            )
        return messages, m, original

    async def preflight(
        self,
        messages: list[ModelMessage],
        settings: ModelSettings | None,
        params: ModelRequestParameters,
    ) -> tuple[int, str]:
        """(input tokens, method) for the request as it would be sent."""
        _, m, _ = await self.fit(messages, settings, params)
        return m.tokens, m.method

    def _log(
        self,
        usage: RequestUsage | None,
        measure: Measure,
        error: str | None = None,
        shrunk_from: int | None = None,
    ) -> None:
        ctx = _call_context.get()
        self.log.write(
            {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "thread_id": ctx.thread_id,
                "purpose": ctx.purpose,
                "model": self.model_name,
                "limit": self.max_input_tokens,
                "preflight_tokens": measure.tokens,
                "preflight_method": measure.method,
                "shrunk_from": shrunk_from,
                "input_tokens": usage.input_tokens if usage else None,
                "cache_read_tokens": usage.cache_read_tokens if usage else None,
                "cache_write_tokens": usage.cache_write_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "within_budget": not self._over(usage.input_tokens) if usage else False,
                "error": error,
            }
        )

    def _check_reported(
        self, usage: RequestUsage, measure: Measure, shrunk_from: int | None
    ) -> None:
        over = self._over(usage.input_tokens)
        error = "reported_over_budget" if over else None
        self._log(usage, measure, error, shrunk_from=shrunk_from)
        if measure.raw > 0 and usage.input_tokens > measure.tokens:  # we judged it too small
            learned = min(usage.input_tokens / measure.raw * 1.02, _MAX_SCALE)
            if learned > self.scale[measure.method]:
                logger.warning(
                    "%s count ran low (%d reported vs %d counted); scale -> %.2f",
                    measure.method,
                    usage.input_tokens,
                    measure.raw,
                    learned,
                )
                self.scale[measure.method] = learned
        if over and self.strict:
            raise BudgetExceededError(usage.input_tokens, self.max_input_tokens, "reported")

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        sent, measure, shrunk_from = await self.fit(
            messages, model_settings, model_request_parameters
        )
        response = await self.wrapped.request(sent, model_settings, model_request_parameters)
        self._check_reported(response.usage, measure, shrunk_from)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncGenerator[StreamedResponse]:
        sent, measure, shrunk_from = await self.fit(
            messages, model_settings, model_request_parameters
        )
        async with self.wrapped.request_stream(
            sent, model_settings, model_request_parameters, run_context
        ) as stream:
            yield stream
        self._check_reported(stream.usage(), measure, shrunk_from)
