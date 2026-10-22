"""The bounded memory worker behind `ask_memory`, `update_memory` and `forget`.

One agent design, three modes that share tools and principles (prompts: `worker.md` + one file
per mode). Fresh per task, no recursion, for the trusted caller only:
  read   - search/read/grep/versions; answers with status, sources and caveats;
  write  - the same, plus save/revise, so it can find what a new statement replaces;
  forget - the same (grep covers every conversation), plus erase.
Bounds: a request limit, a budget of tool-output characters (each request then stays well under
MAX_INPUT_TOKENS), a capped answer. Sources are kept only if the worker actually saw them in a
tool result; "found" without any surviving source is downgraded. Writes cite the user's current
message and are idempotent per (turn, tool, arguments); erase delegates to the deterministic
hard delete in memory/forget.py.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits
from sqlmodel import col, select

from chatbot.agent.budget import call_context
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import forget as forgetting
from chatbot.memory import grep as grepping
from chatbot.memory import records, refs
from chatbot.memory.db import session
from chatbot.memory.models import Message
from chatbot.memory.records import MemoryAccessError, MemoryCaller

logger = logging.getLogger(__name__)

Status = Literal["found", "not_found", "conflicting", "incomplete"]
_REF_PATTERN = r"\b(?:msg|rec|psg|note|ckpt):\d+\b"
_PROMPTS = Path(__file__).parent


class Mode(StrEnum):
    READ = "read"
    WRITE = "write"
    FORGET = "forget"


class MemorySource(BaseModel):
    ref: str = Field(description="a ref seen in a tool result, e.g. msg:12 or rec:3")
    excerpt: str = Field(description="short supporting quote")


class MemoryAnswer(BaseModel):
    status: Status
    answer: str
    sources: list[MemorySource] = Field(default_factory=list)
    caveats: str = ""


class WorkResult(BaseModel):
    """Outcome of a write or forget task."""

    status: Literal[
        "saved",
        "revised",
        "unchanged",
        "skipped",
        "erased",
        "nothing_found",
        "partial",
        "incomplete",
    ]
    summary: str


@dataclass
class WorkerDeps:
    caller: MemoryCaller
    store: str
    chars_left: int
    max_requests: int = 6
    mode: Mode = Mode.READ
    current_msg_ref: str | None = None  # cited by writes; trusted, never model-supplied
    seen: set[str] = field(default_factory=set)
    evidence: dict[str, str] = field(default_factory=dict)  # ref -> line, first seen first
    actions: list[str] = field(default_factory=list)  # mutations done, for a step-limit report
    erased: list[str] = field(default_factory=list)  # values deleted (the reply must not echo)
    erased_refs: bool = False

    def last_step(self, ctx: RunContext) -> str | None:
        """When only the answering request is left, read tools stop returning results."""
        if ctx.usage.requests >= self.max_requests - 1:
            return (
                "Step budget used up. Answer now with what you have (status incomplete if needed)."
            )
        return None

    def spend(self, text: str) -> str:
        """Charge a tool result to the budget; record refs (and their lines) it shows."""
        if self.chars_left <= 0:
            return (
                "Tool budget used up. Answer now with what you have (status incomplete if needed)."
            )
        text = text[: self.chars_left]
        self.chars_left -= len(text)
        self.seen.update(re.findall(_REF_PATTERN, text))
        for line in text.splitlines():
            m = re.match(_REF_PATTERN, line.strip())
            if m and m.group(0) not in self.evidence:
                self.evidence[m.group(0)] = line.strip()
        return text

    def key(self, tool: str, *parts: object) -> str | None:
        """Idempotency key for a write: the turn + tool + arguments."""
        if not self.current_msg_ref:
            return None
        digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode())
        return f"{self.current_msg_ref}|worker-{tool}|{digest.hexdigest()[:24]}"


SearchDeps = WorkerDeps  # the read-only worker's name before it grew modes


# --- formatting -----------------------------------------------------------------------------


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


def _shown(view: records.RecordView) -> str:
    return f"{view.ref} ({view.store} {view.kind} '{view.key}'): {view.text}"


# --- tools (read-only, every mode) ------------------------------------------------------------


def search(ctx: RunContext[WorkerDeps], query: str, k: int = 5) -> str:
    """Keyword (BM25) search over memory records, this conversation's messages and notes."""
    if stop := ctx.deps.last_step(ctx):
        return stop
    try:
        hits = records.search_memory(ctx.deps.caller, query, store=ctx.deps.store, k=k)
    except MemoryAccessError as exc:
        return f"Error: {exc}"
    return ctx.deps.spend(_fmt_hits(hits))


def grep(ctx: RunContext[WorkerDeps], text: str) -> str:
    """Exact, case-insensitive text scan: finds a number, code, name or phrase that keyword
    search ranks badly, and counts every place it occurs. In a forget task it covers all
    conversations, saved outputs and sent messages."""
    if stop := ctx.deps.last_step(ctx):
        return stop
    deps = ctx.deps
    stores = frozenset({"session", "long_term"} if deps.store == "both" else {deps.store})
    user_wide = deps.mode == Mode.FORGET
    found = grepping.find(
        deps.caller.user_id,
        text,
        session_id=None if user_wide else deps.caller.session_id,
        stores=stores,
    )
    return deps.spend(found.render())


def list_records(ctx: RunContext[WorkerDeps], kind: str | None = None, k: int = 10) -> str:
    """The newest current records (all topics), optionally one kind: fact, preference or task."""
    if stop := ctx.deps.last_step(ctx):
        return stop
    try:
        views = records.list_current(ctx.deps.caller, store=ctx.deps.store, kind=kind, k=k)
    except MemoryAccessError as exc:
        return f"Error: {exc}"
    lines = [f"{_shown(v)} [{v.created_at:%Y-%m-%d}]" for v in views]
    return ctx.deps.spend("\n".join(lines) or "No records.")


def read(ctx: RunContext[WorkerDeps], ref: str) -> str:
    """Read one record, message, note or passage by ref."""
    if stop := ctx.deps.last_step(ctx):
        return stop
    try:
        return ctx.deps.spend(format_item(records.read(ctx.deps.caller, ref)))
    except MemoryAccessError as exc:
        return f"Error: {exc}"


def expand(ctx: RunContext[WorkerDeps], ref: str, before: int = 2, after: int = 2) -> str:
    """Messages around a message ref in the same conversation (max 5 each side)."""
    if stop := ctx.deps.last_step(ctx):
        return stop
    try:
        views = records.expand(ctx.deps.caller, ref, before=before, after=after)
    except MemoryAccessError as exc:
        return f"Error: {exc}"
    return ctx.deps.spend("\n".join(format_item(v) for v in views))


def versions(ctx: RunContext[WorkerDeps], ref: str) -> str:
    """All versions of a memory record, oldest first, with dates."""
    if stop := ctx.deps.last_step(ctx):
        return stop
    try:
        chain = records.versions(ctx.deps.caller, ref)
    except MemoryAccessError as exc:
        return f"Error: {exc}"
    return ctx.deps.spend("\n\n".join(format_item(v) for v in chain))


# --- tools (write mode) -----------------------------------------------------------------------


def _citations(deps: WorkerDeps, extra: list[str] | None) -> list[str]:
    cited = [s for s in (extra or []) if s.strip()]
    if deps.current_msg_ref and deps.current_msg_ref not in cited:
        cited.insert(0, deps.current_msg_ref)
    return cited


def save(
    ctx: RunContext[WorkerDeps],
    store: Literal["long_term", "session"],
    kind: Literal["fact", "preference", "task"],
    key: str,
    text: str,
    sources: list[str] | None = None,
) -> str:
    """Save a new record when nothing on the topic exists yet (otherwise use revise).

    Args:
        store: long_term for durable facts and standing preferences; session for this
            conversation only.
        kind: fact, preference (how to behave or write) or task (work in progress).
        key: short topic, e.g. "manager" or "spelling".
        text: one self-contained sentence, names, numbers and dates verbatim (max 500 chars).
        sources: extra refs it rests on; the user's current message is always cited.
    """
    deps = ctx.deps
    try:
        clash = [
            v
            for v in records.list_current(deps.caller, store=store, k=records.MAX_K)
            if v.key == records.normalize_key(key) and v.kind == kind
        ]
        if clash:
            return f"Not saved: {clash[0].ref} is already the current '{key}' record; revise it."
        view = records.write(
            deps.caller,
            store=store,
            kind=kind,
            key=key,
            text=text,
            sources=_citations(deps, sources),
            idempotency_key=deps.key("save", store, kind, key, text),
        )
    except MemoryAccessError as exc:
        return f"Not saved: {exc}"
    deps.actions.append(f"saved {_shown(view)}")
    return f"Saved {_shown(view)}"


def revise(
    ctx: RunContext[WorkerDeps], record: str, text: str, sources: list[str] | None = None
) -> str:
    """Replace the current version of a record with a corrected one; the old version stays as
    history. Give the complete new truth, not a diff.

    Args:
        record: ref of the current version (e.g. rec:12, from search or list_records).
        text: one self-contained sentence (max 500 chars).
        sources: extra refs it rests on; the user's current message is always cited.
    """
    deps = ctx.deps
    try:
        old = records.read(deps.caller, record)
        if not isinstance(old, records.RecordView):
            return f"Not saved: {record} is not a memory record."
        view = records.write(
            deps.caller,
            store=old.store,
            kind=old.kind,
            key=old.key,
            text=text,
            sources=_citations(deps, sources),
            supersedes=record,
            idempotency_key=deps.key("revise", record, text),
        )
    except MemoryAccessError as exc:
        return f"Not saved: {exc}"
    deps.actions.append(f"revised {record} -> {_shown(view)}")
    return f"Saved {_shown(view)} (replaces {record})"


# --- tools (forget mode) ----------------------------------------------------------------------


def erase(
    ctx: RunContext[WorkerDeps],
    values: list[str],
    messages: list[str] | None = None,
    many_ok: bool = False,
) -> str:
    """Permanently delete, from every conversation, everything containing these strings, and
    anything derived from it (records, notes, saved outputs, sent messages). Cannot be undone.

    Args:
        values: distinctive strings (at least 5 characters), e.g. "27 Larkspur Lane".
        messages: refs (msg:N) of messages that discuss the thing without a distinctive string;
            the whole turn is deleted.
        many_ok: set only if the user asked to erase everything about a name or word that
            appears in many messages.
    """
    deps = ctx.deps
    user_id = deps.caller.user_id
    limit = get_settings().memory_erase_max_messages
    clean, short = forgetting.clean_values(values)
    accepted, generic = [], []
    for value in clean:
        count = len(grepping.message_ids(user_id, value))
        if count > limit and not many_ok:
            generic.append(f"{value!r} matches {count} messages")
        else:
            accepted.append(value)
    ids = _own_message_ids(user_id, messages or [])
    if not accepted and not ids:
        problems = [f"too short: {v!r}" for v in short] + generic
        return "Nothing erased. " + ("; ".join(problems) or "No values or messages given.")
    report = forgetting.forget_values(user_id, accepted, message_ids=ids, compact=False)
    deps.erased.extend(accepted)
    deps.erased_refs = deps.erased_refs or bool(ids)
    left = [v for v in accepted if grepping.find(user_id, v).total()]
    parts = {
        "messages": report.messages,
        "records": report.records,
        "notes": report.notes,
        "saved outputs": report.tool_outputs + report.checkpoints,
        "sent messages": report.outbound,
        "stored runs redacted": report.runs_redacted,
    }
    done = ", ".join(f"{n} {name}" for name, n in parts.items() if n) or "nothing stored matched"
    deps.actions.append(f"erased: {done}")
    notes = []
    if short:
        notes.append(f"too short, skipped: {len(short)}")
    if generic:
        notes.append("refused as too generic (use something more specific): " + "; ".join(generic))
    if left:
        notes.append(f"STILL FOUND after erasing: {left}")
    return f"Erased: {done}." + (" " + " ".join(notes) if notes else "")


def _own_message_ids(user_id: str, message_refs: list[str]) -> list[int]:
    wanted = [p.id for r in message_refs if (p := refs.parse(r)) and p.type == "msg"]
    if not wanted:
        return []
    with session() as s:
        rows = s.exec(
            select(Message.id).where(Message.user_id == user_id, col(Message.id).in_(wanted))
        ).all()
    return list(rows)


# --- agents -----------------------------------------------------------------------------------

_COMMON = (grep, list_records, search, read, expand, versions)
_TOOLS = {
    Mode.READ: _COMMON,
    Mode.WRITE: (*_COMMON, save, revise),
    Mode.FORGET: (*_COMMON, erase),
}


@cache
def get_worker(mode: Mode) -> Agent[WorkerDeps, MemoryAnswer | WorkResult]:
    instructions = "\n\n".join(
        (_PROMPTS / name).read_text(encoding="utf-8").strip()
        for name in ("worker.md", f"worker_{mode.value}.md")
    )
    agent = Agent(
        build_model(get_settings()),
        deps_type=WorkerDeps,
        output_type=MemoryAnswer if mode == Mode.READ else WorkResult,
        instructions=instructions,
        # Each step of one task resends the previous steps: automatic caching makes those
        # reads (measured: 604k uncached subagent input tokens in one full scenario run).
        model_settings={"anthropic_cache": True},
    )
    for tool in _TOOLS[mode]:
        agent.tool(tool)
    return agent


def get_search_agent() -> Agent[WorkerDeps, MemoryAnswer]:
    return get_worker(Mode.READ)


get_search_agent.cache_clear = get_worker.cache_clear  # type: ignore[attr-defined]


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
    deps = WorkerDeps(
        caller, store, settings.memory_search_tool_chars, settings.memory_search_max_requests
    )
    try:
        with call_context(purpose="memory_search"):
            result = await get_worker(Mode.READ).run(
                f"Question: {question[:1000]}\nStore: {store}",
                deps=deps,
                usage_limits=UsageLimits(request_limit=settings.memory_search_max_requests),
            )
        answer = result.output
    except UsageLimitExceeded:
        logger.info("memory search hit its step limit")
        closest = list(deps.evidence.items())[:3]
        answer = MemoryAnswer(
            status="incomplete",
            answer="The memory search ran out of steps; the closest evidence it saw is listed.",
            sources=[MemorySource(ref=ref, excerpt=line) for ref, line in closest],
            caveats="Unverified: read the sources, or ask a narrower question.",
        )
    final = _finalize(answer, deps.seen, settings.memory_answer_chars)
    logger.info(
        "ask_memory status=%s sources=%d chars_used=%d",
        final.status,
        len(final.sources),
        settings.memory_search_tool_chars - deps.chars_left,
    )
    return final


async def work(
    mode: Mode,
    caller: MemoryCaller,
    task: str,
    *,
    store: str = "both",
    current_msg_ref: str | None = None,
) -> tuple[WorkResult, WorkerDeps]:
    """Run a write or forget task; returns the outcome and what the worker did."""
    settings = get_settings()
    limit = (
        settings.memory_forget_max_requests
        if mode == Mode.FORGET
        else settings.memory_write_max_requests
    )
    deps = WorkerDeps(
        caller,
        store,
        settings.memory_search_tool_chars,
        limit,
        mode=mode,
        current_msg_ref=current_msg_ref,
    )
    try:
        with call_context(purpose=f"memory_{mode.value}"):
            result = await get_worker(mode).run(
                task[:1500], deps=deps, usage_limits=UsageLimits(request_limit=limit)
            )
        outcome = result.output
    except UsageLimitExceeded:
        logger.info("memory %s hit its step limit", mode.value)
        done = "; ".join(deps.actions) or "nothing was changed"
        outcome = WorkResult(status="incomplete", summary=f"Ran out of steps. Done: {done}.")
    outcome = WorkResult(
        status=outcome.status, summary=records._clip(outcome.summary, settings.memory_answer_chars)
    )
    if deps.erased:  # the summary must never echo what was forgotten
        outcome.summary = forgetting.redact(outcome.summary, deps.erased)
    logger.info("memory %s status=%s actions=%d", mode.value, outcome.status, len(deps.actions))
    return outcome, deps


def render(answer: MemoryAnswer) -> str:
    lines = [f"status: {answer.status}", f"answer: {answer.answer}"]
    if answer.sources:
        lines.append("sources:")
        lines += [f"- {s.ref}: {s.excerpt}" for s in answer.sources]
    if answer.caveats:
        lines.append(f"caveats: {answer.caveats}")
    return "\n".join(lines)


def render_work(outcome: WorkResult) -> str:
    return f"status: {outcome.status}\nsummary: {outcome.summary}"
