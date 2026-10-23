"""Document tools: list available ids, fetch text/notes, and search passages."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
from pydantic_ai import RunContext
from sqlmodel import select

from chatbot.capabilities.registry import capability
from chatbot.config import get_settings
from chatbot.docs_reader import reader
from chatbot.memory.db import session
from chatbot.memory.models import DocRef


def _unknown(doc_id: str) -> str:
    names = ", ".join(sorted(set(reader.available()))[:10]) or "(none)"
    return (
        f"No document named {doc_id!r}. Available documents (first 10): {names}. "
        "Use list_docs to browse all available ids."
    )


def list_docs(
    ctx: RunContext,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=10)] = 10,
) -> dict[str, object]:
    """List document ids alphabetically without reading contents.

    Pass an id to fetch_doc or search_doc. If next_offset is not null, call list_docs
    with that offset for the next page.
    """
    names = sorted(set(reader.available()))
    page = names[offset : offset + limit]
    end = offset + len(page)
    return {
        "doc_ids": page,
        "total": len(names),
        "next_offset": end if end < len(names) else None,
    }


def remember_ref(thread_id: str, doc_id: str) -> None:
    with session() as s:
        ref = s.exec(
            select(DocRef).where(DocRef.thread_id == thread_id, DocRef.doc_id == doc_id)
        ).first()
        ref = ref or DocRef(thread_id=thread_id, doc_id=doc_id)
        ref.last_used_at = datetime.now(UTC)
        s.add(ref)
        s.commit()


async def fetch_doc(ctx: RunContext, doc_id: str) -> str:
    """Read a document by id: full text if short, otherwise a summary with key facts verbatim.

    Use search_doc for details missing from the note.
    """
    try:
        doc = reader.load(doc_id)
    except reader.DocNotFoundError:
        return _unknown(doc_id)
    remember_ref(ctx.deps.thread_id, doc.doc_id)
    if doc.approx_tokens <= get_settings().doc_full_text_tokens:
        return f"Document {doc.doc_id} (full text):\n\n{doc.text}"
    reader.ensure_indexed(doc)
    note = await reader.get_note(doc)
    return (
        f"Document {doc.doc_id} (~{doc.approx_tokens:,} tokens; too long to read whole). "
        f"Note distilled from the full text:\n\n{note}"
    )


def search_doc(ctx: RunContext, doc_id: str, query: str) -> str:
    """Find document passages matching a question or keywords."""
    try:
        doc = reader.load(doc_id)
    except reader.DocNotFoundError:
        return _unknown(doc_id)
    remember_ref(ctx.deps.thread_id, doc.doc_id)
    found = reader.find_passages(doc, query)
    if not found:
        return f"No passages in {doc.doc_id} match {query!r}."
    return "\n\n".join(f"[{doc.doc_id} § {p.section}]\n{p.text}" for p in found)


CAPABILITY = capability(
    "documents",
    "List, read and search the user's documents.",
    __file__,
    list_docs,
    fetch_doc,
    search_doc,
)
