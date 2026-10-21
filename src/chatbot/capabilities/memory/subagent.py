"""The bounded, read-only memory subagent behind `ask_memory` (docs/MEMORY_CONTRACTS.md §5).

Fresh per question, no recursion, read-only tools over the memory store for the trusted caller
only. Bounds: a request limit, a total budget of tool-output characters (each request then
stays well under MAX_INPUT_TOKENS), and a capped answer. Sources are kept only if the subagent
actually saw them in a tool result; "found" without any surviving source is downgraded.
"""

import logging
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from chatbot.agent.budget import call_context
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import records
from chatbot.memory.records import MemoryAccessError, MemoryCaller

logger = logging.getLogger(__name__)

Status = Literal["found", "not_found", "conflicting", "incomplete"]
_REF_PATTERN = r"\b(?:msg|rec|psg|note|ckpt):\d+\b"


class MemorySource(BaseModel):
    ref: str = Field(description="a ref seen in a tool result, e.g. msg:12 or rec:3")
    excerpt: str = Field(description="short supporting quote")


class MemoryAnswer(BaseModel):
    status: Status
    answer: str
    sources: list[MemorySource] = Field(default_factory=list)
    caveats: str = ""


@dataclass
class SearchDeps:
    caller: MemoryCaller
    store: str
    chars_left: int
    seen: set[str] = field(default_factory=set)

    def spend(self, text: str) -> str:
        """Charge a tool result to the budget; record refs it shows."""
        if self.chars_left <= 0:
            return (
                "Tool budget used up. Answer now with what you have (status incomplete if needed)."
            )
        text = text[: self.chars_left]
        self.chars_left -= len(text)
        self.seen.update(re.findall(_REF_PATTERN, text))
        return text


def _fmt_hits(hits: list[records.Hit]) -> str:
    if not hits:
        return "No results."
    lines = []
    for h in hits:
        meta = [h.type, h.store, h.created_at.strftime("%Y-%m-%d")]
        if h.type == "record":
            meta.append("current" if h.current else "superseded")
        if h.covers:
            meta.append(f"covers {h.covers}")
        lines.append(f"{h.ref} ({', '.join(meta)}): {h.excerpt}")
    return "\n".join(lines)


def format_item(item) -> str:
    if isinstance(item, records.RecordView):
        links = []
        if item.supersedes:
            links.append(f"replaces {item.supersedes}")
        if item.superseded_by:
            links.append(f"replaced by {item.superseded_by}")
        status = "current" if item.current else "superseded"
        src = "; ".join(f'{s.ref}: "{s.excerpt}"' for s in item.sources)
        when = f"{item.created_at:%Y-%m-%d}"
        head = f"{item.ref} [{item.store} {item.kind} '{item.key}', {status}, {when}"
        tail = f"{', ' + ', '.join(links) if links else ''}]"
        return f"{head}{tail}\n{item.text}\nsaved from (evidence excerpts): {src}"
    if isinstance(item, records.MessageView):
        return f"{item.ref} [turn {item.turn}, {item.role}, {item.created_at:%Y-%m-%d}] {item.text}"
    if isinstance(item, records.NoteView):
        return f"{item.ref} [note, covers {', '.join(item.covers)}]\n{item.text}"
    if isinstance(item, records.SavedView):
        return f"{item.ref} [{item.label}, {item.created_at:%Y-%m-%d}]\n{item.text}"
    if isinstance(item, records.PassageView):
        return f"{item.ref} [document {item.doc_id} § {item.section}]\n{item.text}"
    return str(item)


@cache
def get_search_agent() -> Agent[SearchDeps, MemoryAnswer]:
    agent = Agent(
        build_model(get_settings()),
        deps_type=SearchDeps,
        output_type=MemoryAnswer,
        instructions=(Path(__file__).parent / "search_agent.md").read_text(encoding="utf-8"),
        # Each step of one question resends the previous steps: automatic caching makes those
        # reads (measured: 604k uncached subagent input tokens in one full scenario run).
        model_settings={"anthropic_cache": True},
    )

    @agent.tool
    def search(ctx: RunContext[SearchDeps], query: str, k: int = 5) -> str:
        """Keyword (BM25) search over memory records, this conversation's messages and notes."""
        try:
            hits = records.search_memory(ctx.deps.caller, query, store=ctx.deps.store, k=k)
        except MemoryAccessError as exc:
            return f"Error: {exc}"
        return ctx.deps.spend(_fmt_hits(hits))

    @agent.tool
    def read(ctx: RunContext[SearchDeps], ref: str) -> str:
        """Read one record, message, note or passage by ref."""
        try:
            return ctx.deps.spend(format_item(records.read(ctx.deps.caller, ref)))
        except MemoryAccessError as exc:
            return f"Error: {exc}"

    @agent.tool
    def expand(ctx: RunContext[SearchDeps], ref: str, before: int = 2, after: int = 2) -> str:
        """Messages around a message ref in the same conversation (max 5 each side)."""
        try:
            views = records.expand(ctx.deps.caller, ref, before=before, after=after)
        except MemoryAccessError as exc:
            return f"Error: {exc}"
        return ctx.deps.spend("\n".join(format_item(v) for v in views))

    @agent.tool
    def versions(ctx: RunContext[SearchDeps], ref: str) -> str:
        """All versions of a memory record, oldest first, with dates."""
        try:
            chain = records.versions(ctx.deps.caller, ref)
        except MemoryAccessError as exc:
            return f"Error: {exc}"
        return ctx.deps.spend("\n\n".join(format_item(v) for v in chain))

    return agent


def _finalize(answer: MemoryAnswer, seen: set[str], limit: int) -> MemoryAnswer:
    kept = [
        MemorySource(ref=s.ref, excerpt=records._clip(s.excerpt, records.MAX_EXCERPT))
        for s in answer.sources
        if s.ref in seen
    ][:8]
    status, caveats = answer.status, answer.caveats
    if status in ("found", "conflicting") and not kept:
        status = "incomplete"
        caveats = (caveats + " No cited source could be verified.").strip()
    return MemoryAnswer(
        status=status,
        answer=records._clip(answer.answer, limit),
        sources=kept,
        caveats=records._clip(caveats, 600) if caveats else "",
    )


async def ask(caller: MemoryCaller, question: str, store: str = "both") -> MemoryAnswer:
    settings = get_settings()
    if store not in ("session", "long_term", "both"):
        store = "both"
    deps = SearchDeps(caller, store, settings.memory_search_tool_chars)
    try:
        with call_context(purpose="memory_search"):
            result = await get_search_agent().run(
                f"Question: {question[:1000]}\nStore: {store}",
                deps=deps,
                usage_limits=UsageLimits(request_limit=settings.memory_search_max_requests),
            )
        answer = result.output
    except UsageLimitExceeded:
        logger.info("memory search hit its step limit")
        answer = MemoryAnswer(
            status="incomplete",
            answer="The memory search ran out of steps before reaching an answer.",
            caveats="Ask a narrower question or search again.",
        )
    final = _finalize(answer, deps.seen, settings.memory_answer_chars)
    logger.info(
        "ask_memory status=%s sources=%d chars_used=%d",
        final.status,
        len(final.sources),
        settings.memory_search_tool_chars - deps.chars_left,
    )
    return final


def render(answer: MemoryAnswer) -> str:
    lines = [f"status: {answer.status}", f"answer: {answer.answer}"]
    if answer.sources:
        lines.append("sources:")
        lines += [f"- {s.ref}: {s.excerpt}" for s in answer.sources]
    if answer.caveats:
        lines.append(f"caveats: {answer.caveats}")
    return "\n".join(lines)
