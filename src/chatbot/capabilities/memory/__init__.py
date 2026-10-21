"""Memory capability: `ask_memory` (read-only subagent) and explicit, validated writes.

The caller (user + session) and the current message come from trusted run dependencies; the
model only supplies the question or the record content. Writes are idempotent per tool call.
"""

from typing import Literal

from pydantic_ai import RunContext

from chatbot.capabilities.memory import subagent
from chatbot.capabilities.registry import capability
from chatbot.memory import records
from chatbot.memory.records import MemoryAccessError, MemoryCaller


def caller_of(ctx: RunContext) -> MemoryCaller:
    return MemoryCaller(ctx.deps.user_id, ctx.deps.thread_id)


def _sources(ctx: RunContext, sources: list[str] | None) -> list[str]:
    """Explicit refs, or the user's current message (the usual evidence for a statement)."""
    cited = [s for s in (sources or []) if s.strip()]
    current = getattr(ctx.deps, "current_msg_ref", None)
    if current and current not in cited:
        cited.insert(0, current)
    return cited


def _shown(view: records.RecordView) -> str:
    return f"Saved {view.ref} ({view.store} {view.kind} '{view.key}'): {view.text}"


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


def remember(
    ctx: RunContext,
    store: Literal["session", "long_term"],
    kind: Literal["fact", "preference", "task"],
    key: str,
    text: str,
    sources: list[str] | None = None,
) -> str:
    """Remember something new. Use update_memory instead to change an existing record.

    Args:
        store: "long_term" for durable facts and standing preferences, "session" for things
            that only apply to this conversation.
        kind: "fact", "preference" (how to behave or write) or "task" (work in progress).
        key: short topic, e.g. "manager" or "spelling".
        text: one self-contained sentence, names and dates verbatim (max 500 chars).
        sources: extra refs this rests on; the user's current message is always included.
    """
    try:
        view = records.write(
            caller_of(ctx),
            store=store,
            kind=kind,
            key=key,
            text=text,
            sources=_sources(ctx, sources),
            idempotency_key=ctx.tool_call_id,
        )
    except MemoryAccessError as exc:
        return f"Not saved: {exc}"
    return _shown(view)


def update_memory(ctx: RunContext, record: str, text: str, sources: list[str] | None = None) -> str:
    """Replace a remembered record with a new version; the old one stays as history.

    Args:
        record: ref of the current version (e.g. "rec:12", from ask_memory).
        text: the new content, one self-contained sentence (max 500 chars).
        sources: extra refs; the user's current message is always included.
    """
    caller = caller_of(ctx)
    try:
        old = records.read(caller, record)
        if not isinstance(old, records.RecordView):
            return f"Not saved: {record} is not a memory record."
        view = records.write(
            caller,
            store=old.store,
            kind=old.kind,
            key=old.key,
            text=text,
            sources=_sources(ctx, sources),
            supersedes=record,
            idempotency_key=ctx.tool_call_id,
        )
    except MemoryAccessError as exc:
        return f"Not saved: {exc}"
    return _shown(view) + f" (replaces {record})"


CAPABILITY = capability(
    "memory",
    "Recall from and write to the user's memory.",
    __file__,
    ask_memory,
    remember,
    update_memory,
)
