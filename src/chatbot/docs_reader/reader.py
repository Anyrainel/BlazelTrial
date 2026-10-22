"""Load documents from DOCS_DIR, index their passages, and distill a persistent doc note.

A document never enters a prompt whole unless it is small. Large ones are:
  * split into passages (~1k chars, section-aware) indexed for `search_doc`;
  * read once by map-reduce (chunk notes -> one doc note with verbatim key facts), cached in the
    DB per content hash, so later questions cost one small tool result instead of a re-read.
"""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from pydantic_ai import Agent
from sqlmodel import col, delete, select

from chatbot import prompts
from chatbot.agent.budget import call_context
from chatbot.agent.models import build_model, mechanical_task_settings
from chatbot.config import get_settings
from chatbot.memory import search
from chatbot.memory.db import session
from chatbot.memory.models import DocNote, DocPassage

_DOC_EXTENSIONS = (".md", ".txt")
_ID_PATTERN = re.compile(r"^[\w.\- ]{1,100}$")


class DocNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Doc:
    doc_id: str  # canonical id = file stem
    text: str
    sha256: str

    @property
    def approx_tokens(self) -> int:
        return int(len(self.text) / get_settings().estimate_chars_per_token)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def available() -> list[str]:
    root = get_settings().docs_path
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.iterdir() if p.suffix in _DOC_EXTENSIONS and p.is_file())


def resolve(doc_id: str) -> Path:
    """Find the file for `doc_id`, forgiving case/spacing/punctuation ("q3 plan" -> Q3-plan.md).

    Only names listed in DOCS_DIR can match, so no path from the model is ever opened directly.
    """
    if not _ID_PATTERN.match(doc_id):
        raise DocNotFoundError(doc_id)
    wanted = _norm(Path(doc_id).stem if Path(doc_id).suffix in _DOC_EXTENSIONS else doc_id)
    root = get_settings().docs_path
    for stem in available():
        if _norm(stem) == wanted:
            for ext in _DOC_EXTENSIONS:
                path = root / f"{stem}{ext}"
                if path.is_file():
                    return path
    raise DocNotFoundError(doc_id)


def load(doc_id: str) -> Doc:
    path = resolve(doc_id)
    text = path.read_text(encoding="utf-8")
    return Doc(path.stem, text, hashlib.sha256(text.encode()).hexdigest())


def sections(text: str) -> list[tuple[str, list[str]]]:
    """[(heading, paragraphs)] from markdown; text before any heading goes under ""."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            first, _, rest = block.partition("\n")
            out.append((first.lstrip("# ").strip(), []))
            if rest.strip():
                out[-1][1].append(rest.strip())
        else:
            out[-1][1].append(block)
    return [(h, ps) for h, ps in out if ps]


def passages(text: str, max_chars: int) -> list[tuple[str, str]]:
    """[(section, passage)]: paragraphs grouped within a section up to `max_chars`."""
    out = []
    for heading, paras in sections(text):
        buf = ""
        for para in paras:
            while len(para) > max_chars:  # split an oversized paragraph
                out.append((heading, para[:max_chars]))
                para = para[max_chars:]
            if buf and len(buf) + len(para) + 1 > max_chars:
                out.append((heading, buf))
                buf = ""
            buf = f"{buf}\n{para}" if buf else para
        if buf:
            out.append((heading, buf))
    return out


def chunks(text: str, max_chars: int) -> list[str]:
    """Consecutive passages (with their headings) grouped up to `max_chars` for the map step."""
    out, buf, last_heading = [], "", None
    for heading, passage in passages(text, max_chars):
        piece = passage if heading == last_heading else f"## {heading}\n{passage}"
        if buf and len(buf) + len(piece) + 2 > max_chars:
            out.append(buf)
            buf = f"## {heading}\n{passage}"
        else:
            buf = f"{buf}\n\n{piece}" if buf else piece
        last_heading = heading
    if buf:
        out.append(buf)
    return out


def ensure_indexed(doc: Doc) -> None:
    """Index this version's passages once; drop passages of older versions."""
    settings = get_settings()
    with session() as s:
        if s.exec(select(DocPassage.id).where(DocPassage.sha256 == doc.sha256)).first():
            return
        stale = s.exec(select(DocPassage.id).where(DocPassage.doc_id == doc.doc_id)).all()
        search.delete_passages([pid for pid in stale if pid is not None])
        s.exec(delete(DocPassage).where(col(DocPassage.doc_id) == doc.doc_id))
        rows = [
            DocPassage(doc_id=doc.doc_id, sha256=doc.sha256, idx=i, section=h, text=t)
            for i, (h, t) in enumerate(passages(doc.text, settings.doc_passage_chars))
        ]
        s.add_all(rows)
        s.commit()
        search.index_passages([(r.id, r.doc_id, r.text) for r in rows if r.id is not None])


def find_passages(doc: Doc, query: str) -> list[DocPassage]:
    """Best passages for `query`, in document order, within the doc search budget."""
    settings = get_settings()
    ensure_indexed(doc)
    ids = search.search_passages(doc.doc_id, query, k=8)
    with session() as s:
        by_id = {p.id: p for p in s.exec(select(DocPassage).where(col(DocPassage.id).in_(ids)))}
    budget = int(settings.doc_search_token_budget * settings.estimate_chars_per_token)
    chosen, used = [], 0
    for pid in ids:  # best first
        p = by_id.get(pid)
        if p is None or used + len(p.text) > budget:
            continue
        chosen.append(p)
        used += len(p.text)
    return sorted(chosen, key=lambda p: p.idx)


# --- notes (map-reduce) ----------------------------------------------------------------------


@cache
def get_mapper() -> Agent[None, str]:
    return Agent(
        build_model(get_settings()),
        instructions=prompts.load("doc_map"),
        model_settings=mechanical_task_settings(get_settings(), max_tokens=1500),
    )


@cache
def get_reducer() -> Agent[None, str]:
    return Agent(
        build_model(get_settings()),
        instructions=prompts.load("doc_reduce"),
        model_settings=mechanical_task_settings(get_settings(), max_tokens=1800),
    )


async def _map(doc: Doc, parts: list[str]) -> list[str]:
    sem = asyncio.Semaphore(get_settings().doc_note_concurrency)

    async def one(i: int, part: str) -> str:
        async with sem:
            prompt = f"Document: {doc.doc_id} — part {i + 1} of {len(parts)}\n\n{part}"
            return (await get_mapper().run(prompt)).output

    return list(await asyncio.gather(*(one(i, p) for i, p in enumerate(parts))))


async def _reduce(doc: Doc, notes: list[str], max_chars: int) -> str:
    """Combine notes in groups until one remains.

    Every group takes at least two notes, each clipped to half the group size, so the count at
    least halves per round and the loop always ends (log2(n) rounds).
    """
    half = max_chars // 2
    while True:
        clipped = [n if len(n) <= half else n[:half] + " …" for n in notes]
        groups: list[list[str]] = []
        for note in clipped:
            last = groups[-1] if groups else None
            if last is not None and (
                len(last) < 2 or sum(len(n) for n in last) + len(note) <= max_chars
            ):
                last.append(note)
            else:
                groups.append([note])
        if len(groups) > 1 and len(groups[-1]) == 1:  # never leave a lone note behind
            groups[-2].extend(groups.pop())
        outs = []
        for group in groups:
            body = "\n\n".join(f"[Part notes {i + 1}]\n{n}" for i, n in enumerate(group))
            outs.append((await get_reducer().run(f"Document: {doc.doc_id}\n\n{body}")).output)
        if len(outs) == 1:
            return outs[0]
        notes = outs


_note_locks: dict[str, asyncio.Lock] = {}


def _cached_note(sha256: str) -> str | None:
    with session() as s:
        cached = s.exec(select(DocNote).where(DocNote.sha256 == sha256)).first()
    return cached.text if cached else None


async def get_note(doc: Doc) -> str:
    """The doc note for this version, computed once and cached in the DB. Concurrent requests
    for the same document (e.g. parallel tool calls) wait for one computation."""
    if note := _cached_note(doc.sha256):
        return note
    async with _note_locks.setdefault(doc.sha256, asyncio.Lock()):
        if note := _cached_note(doc.sha256):
            return note
        return await _compute_note(doc)


async def _compute_note(doc: Doc) -> str:
    settings = get_settings()
    # Leave half the budget for instructions, framing and output.
    max_chars = int(
        min(settings.doc_chunk_tokens, settings.max_input_tokens // 2)
        * settings.estimate_chars_per_token
    )
    parts = chunks(doc.text, max_chars)
    with call_context(purpose="doc_note"):
        note = await _reduce(doc, await _map(doc, parts), max_chars)
    with session() as s:
        s.add(DocNote(doc_id=doc.doc_id, sha256=doc.sha256, text=note, chunk_count=len(parts)))
        s.commit()
    return note
