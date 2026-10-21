"""Document tools: fetch_doc (full text or distilled note) and search_doc (passages)."""

from datetime import UTC, datetime

from pydantic_ai import RunContext
from sqlmodel import select

from chatbot.capabilities.registry import capability
from chatbot.config import get_settings
from chatbot.docs_reader import reader
from chatbot.memory.db import session
from chatbot.memory.models import DocRef


def _unknown(doc_id: str) -> str:
    names = ", ".join(reader.available()) or "(none)"
    return f"No document named {doc_id!r}. Available documents: {names}."


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
    """Read a document from the user's documents folder by its id (e.g. "Q3-plan").

    Short documents come back in full. Long ones come back as a note: a summary plus the key
    facts (dates, numbers, names) verbatim. Use search_doc for details the note doesn't cover.
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
    """Find the passages of a document most relevant to a question or keywords. Use it for
    details that a fetch_doc note doesn't include."""
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
    "documents", "Read and search the user's documents.", __file__, fetch_doc, search_doc
)
