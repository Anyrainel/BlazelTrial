"""History processor: keep every request of a turn under budget, including long tool loops.

Runs before each model request. If the estimate exceeds the target:
1. drop the oldest replayed turns, whole (a tool call never loses its return);
2. if the current run alone is still too big, move its completed tool results out of context:
   each is saved (`out:` ref, idempotent per tool call) and replaced by a short stub naming
   the ref, and the run's progress is written to a checkpoint (`ckpt:` ref). The most recent
   results stay inline so the model can act on them. Call/return pairs are preserved, and
   since the processed history replaces the run's state, later requests keep the stubs.
The budget guard on the model remains the hard stop.
"""

import json
import logging
from dataclasses import replace

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from chatbot.agent.budget import estimate_tokens
from chatbot.config import get_settings
from chatbot.memory import records

logger = logging.getLogger(__name__)

STUB_PREFIX = "[saved as "
_STUB_PREVIEW = 200
_KEEP_RECENT_RETURNS = 1


def _turn_starts(messages: list[ModelMessage]) -> list[int]:
    """Indexes where a turn begins: a request carrying a user prompt."""
    return [
        i
        for i, m in enumerate(messages)
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)
    ]


def _calls_by_id(messages: list[ModelMessage]) -> dict[str, ToolCallPart]:
    return {
        p.tool_call_id: p
        for m in messages
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart)
    }


def _externalize(
    ctx: RunContext, messages: list[ModelMessage], start: int, over
) -> list[ModelMessage]:
    """Replace completed tool results of the current run (from `start`) with saved-ref stubs,
    oldest first, until the history fits. Writes/refreshes the run checkpoint."""
    caller = records.MemoryCaller(ctx.deps.user_id, ctx.deps.thread_id)
    returns = [
        (i, j)
        for i in range(start, len(messages))
        if isinstance(messages[i], ModelRequest)
        for j, p in enumerate(messages[i].parts)
        if isinstance(p, ToolReturnPart)
    ]
    candidates = returns[:-_KEEP_RECENT_RETURNS] if len(returns) > _KEEP_RECENT_RETURNS else []
    calls = _calls_by_id(messages)
    out = list(messages)
    changed = False
    for i, j in candidates:
        if not over(out):
            break
        part = out[i].parts[j]
        text = part.model_response_str()
        if text.startswith(STUB_PREFIX):
            continue
        ref = records.save_tool_output(caller, part.tool_call_id, part.tool_name, text)
        preview = " ".join(text.split())[:_STUB_PREVIEW]
        stub = f"{STUB_PREFIX}{ref}; read_memory('{ref}') for the full text] {preview}…"
        parts = list(out[i].parts)
        parts[j] = replace(part, content=stub)
        out[i] = replace(out[i], parts=parts)
        changed = True
    if changed:
        records.save_checkpoint(caller, ctx.run_id, _checkpoint_text(out[start:], calls))
        logger.info("compacted current run %s in %s", ctx.run_id, ctx.deps.thread_id)
    return out


def _checkpoint_text(run: list[ModelMessage], calls: dict[str, ToolCallPart]) -> str:
    goal = next(
        (
            p.content if isinstance(p.content, str) else " ".join(map(str, p.content))
            for m in run
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
        ),
        "",
    )
    lines = [f"Goal: {goal[:500]}", "Completed tool calls:"]
    for m in run:
        if not isinstance(m, ModelRequest):
            continue
        for p in m.parts:
            if isinstance(p, ToolReturnPart):
                call = calls.get(p.tool_call_id)
                args = json.dumps(call.args_as_dict())[:200] if call else "?"
                result = p.model_response_str()
                where = (
                    result.split(";")[0][len(STUB_PREFIX) :]
                    if result.startswith(STUB_PREFIX)
                    else "inline"
                )
                lines.append(f"- {p.tool_name}({args}) -> {where}")
    return "\n".join(lines)


def fit_history(ctx: RunContext, messages: list[ModelMessage]) -> list[ModelMessage]:
    settings = get_settings()
    target = settings.context_target_tokens
    # Instructions are counted by the estimate itself; add the tool schemas and the margin.
    from chatbot.agent.agent import tool_schema_tokens

    overhead = tool_schema_tokens() + settings.tool_overhead_tokens
    cpt = settings.estimate_chars_per_token
    params = ModelRequestParameters()

    def over(msgs: list[ModelMessage]) -> bool:
        return estimate_tokens(msgs, params, cpt) + overhead > target

    starts = _turn_starts(messages)
    current = next(
        (i for i in starts if getattr(messages[i], "run_id", None) == ctx.run_id),
        starts[-1] if starts else 0,
    )
    drop = 0
    while over(messages[drop:]):
        later = [i for i in starts if drop < i <= current]
        if not later:
            break  # only the current turn is left; nothing more we may drop
        drop = later[0]
    kept = messages[drop:] if drop else messages
    if over(kept) and getattr(ctx.deps, "thread_id", None):
        kept = _externalize(ctx, kept, current - drop, over)
    return kept
