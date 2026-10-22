"""Memory capability: `ask_memory`, `update_memory` and `forget`, each run by the bounded memory
worker (subagent.py), plus `read_memory` for saved tool outputs.

The caller (user + session) and the current message come from trusted run dependencies; the
model only describes what to find, remember or forget. Writes are idempotent per turn.
"""

import hashlib
import json
from typing import Literal

from pydantic_ai import RunContext

from chatbot.capabilities.memory import subagent
from chatbot.capabilities.registry import capability
from chatbot.memory import forget as forgetting
from chatbot.memory import records
from chatbot.memory.records import MemoryAccessError, MemoryCaller


def caller_of(ctx: RunContext) -> MemoryCaller:
    return MemoryCaller(ctx.deps.user_id, ctx.deps.thread_id)


def mutation_key(ctx: RunContext, tool: str, *parts: object) -> str:
    """Idempotency key for a write: the turn + tool + arguments. A retried run (new tool-call
    ids) that repeats the same write gets the original result instead of a duplicate."""
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    turn = getattr(ctx.deps, "current_msg_ref", None) or ctx.tool_call_id
    return f"{turn}|{tool}|{digest[:24]}"


async def ask_memory(
    ctx: RunContext, question: str, store: Literal["session", "long_term", "both"] = "both"
) -> str:
    """Ask the user's memory a question (people, facts, preferences, earlier messages, documents
    discussed). Returns a status, an answer and the sources it rests on.

    Args:
        question: What to find out; add a short hint if it could be ambiguous.
        store: "session" (this conversation), "long_term" (remembered across conversations)
            or "both".
    """
    answer = await subagent.ask(caller_of(ctx), question, store)
    return subagent.render(answer)


async def update_memory(
    ctx: RunContext, change: str, scope: Literal["long_term", "session"] = "long_term"
) -> str:
    """Remember something new, or change something remembered, in one call. Memory finds what
    the statement replaces and keeps the old version as history.

    Args:
        change: what is now true, in one sentence with names, dates and numbers verbatim, e.g.
            "The user's manager is Priya Raman" or "The user now wants American spelling
            (was British)".
        scope: "long_term" for durable facts and standing preferences (every conversation),
            "session" for what applies only to this conversation.
    """
    key = mutation_key(ctx, "update_memory", scope, change)
    done = ctx.deps.outputs.setdefault("mutations", {})
    if key not in done:  # a retried run repeats the call; the first result stands
        outcome, _ = await subagent.work(
            subagent.Mode.WRITE,
            caller_of(ctx),
            f"Memory update (store: {scope}): {change}",
            store=scope,
            current_msg_ref=getattr(ctx.deps, "current_msg_ref", None),
        )
        done[key] = subagent.render_work(outcome)
    return done[key]


async def forget(ctx: RunContext, what: str) -> str:
    """Forget something the user asked to forget, from every conversation: messages,
    memory, summaries, saved outputs. Display-only chat originals may remain depending on
    configuration. Memory finds every place and wording; do not search first.

    Args:
        what: the thing to forget in the user's words, e.g. "my home address".
    """
    outcome, deps = await subagent.work(
        subagent.Mode.FORGET,
        caller_of(ctx),
        f"Forget: {what}",
        current_msg_ref=getattr(ctx.deps, "current_msg_ref", None),
    )
    ctx.deps.outputs.setdefault("forget", []).extend(deps.erased)  # scrubbed again after the turn
    if deps.erased or deps.erased_refs:
        # The run no longer carries the values, so the post-turn sweep may find nothing left to
        # delete and skip compaction: purge the freed bytes here.
        forgetting.compact_storage()
    note = "Do not repeat the forgotten information in your reply or in any message."
    return f"{subagent.render_work(outcome)}\n{note}"


def read_memory(ctx: RunContext, ref: str) -> str:
    """Read one item by ref: a source cited by ask_memory, or a tool result saved out of this
    conversation's context (out:...) or its checkpoint (ckpt:...).

    Args:
        ref: e.g. "msg:40", "rec:3", "note:2", "psg:17", "out:5", "ckpt:1".
    """
    try:
        return subagent.format_item(records.read(caller_of(ctx), ref))
    except MemoryAccessError as exc:
        return f"Error: {exc}"


CAPABILITY = capability(
    "memory",
    "Recall from and write to the user's memory.",
    __file__,
    ask_memory,
    update_memory,
    read_memory,
    forget,
)
