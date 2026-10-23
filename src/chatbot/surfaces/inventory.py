"""Read-only, owner-filtered inventory for the human-facing artifact browser.

This API is deliberately separate from agent memory tools: humans may inspect all
their sessions, while an agent still has the narrower MemoryCaller visibility.
"""

import json
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import or_
from sqlmodel import SQLModel, col, func, select
from sqlmodel.sql.expression import SelectOfScalar

from chatbot.capabilities.registry import all_capabilities
from chatbot.config import get_settings
from chatbot.memory import models as m
from chatbot.memory.db import session
from chatbot.prompts import load

router = APIRouter(prefix="/agent")
TABLES = {
    "records": (m.MemoryRecord, "Memory records"),
    "sources": (m.RecordSource, "Record sources"),
    "notes": (m.EpisodeNote, "Conversation notes"),
    "documents": (m.DocNote, "Document notes"),
    "passages": (m.DocPassage, "Document passages"),
    "document_refs": (m.DocRef, "Opened documents"),
    "outputs": (m.ToolOutput, "Saved tool outputs"),
    "checkpoints": (m.Checkpoint, "Task checkpoints"),
    "outbound": (m.OutboundMessage, "Messages"),
    "posts": (m.GeneratedPost, "LinkedIn posts"),
    "deliveries": (m.SlackDelivery, "Slack delivery receipts"),
    "legacy_facts": (m.Fact, "Legacy facts"),
    "legacy_summaries": (m.Summary, "Legacy summaries"),
}


def _calls() -> list[dict[str, Any]]:
    """A bounded usage-log view, excluding other owners' conversation calls."""
    settings = get_settings()
    if not settings.llm_log_path.is_file():
        return []
    with session() as s:
        threads = set(
            s.exec(select(m.Thread.id).where(m.Thread.user_id == settings.default_user_id)).all()
        )
    fields = {
        "ts",
        "thread_id",
        "purpose",
        "model",
        "input_tokens",
        "output_tokens",
        "within_budget",
        "preflight_tokens",
        "preflight_method",
        "shrunk_from",
        "error",
    }
    with settings.llm_log_path.open(encoding="utf-8") as stream:
        lines = deque(stream, maxlen=500)
    rows = []
    for i, line in enumerate(reversed(lines)):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a final line still being appended
        if row.get("thread_id") is not None and row["thread_id"] not in threads:
            continue
        rows.append(
            {
                "id": f"{row.get('ts', '')}:{i}",
                **{key: value for key, value in row.items() if key in fields},
            }
        )
    return rows


def _owned(model: type[SQLModel], user_id: str) -> SelectOfScalar:
    query = select(model)
    if hasattr(model, "user_id"):
        return query.where(model.user_id == user_id)
    if model is m.RecordSource:
        return query.join(m.MemoryRecord).where(m.MemoryRecord.user_id == user_id)
    if model is m.SlackDelivery:
        return query.join(m.Message, model.reply_message_id == m.Message.id).where(
            m.Message.user_id == user_id
        )
    if hasattr(model, "thread_id"):
        return query.join(m.Thread, model.thread_id == m.Thread.id).where(
            m.Thread.user_id == user_id
        )
    # Document corpus and cached document notes are shared by the single local user.
    return query


@router.get("/inventory")
def inventory() -> dict[str, Any]:
    user_id = get_settings().default_user_id
    with session() as s:
        categories = [
            {
                "id": key,
                "label": label,
                "count": s.exec(
                    select(func.count()).select_from(_owned(model, user_id).subquery())
                ).one(),
            }
            for key, (model, label) in TABLES.items()
        ]
    categories.append({"id": "model_calls", "label": "Recent model calls", "count": len(_calls())})
    settings = get_settings()
    files = (
        sorted({p.name for p in settings.docs_path.iterdir() if p.is_file()})
        if settings.docs_path.is_dir()
        else []
    )
    return {
        "categories": categories,
        "files": files,
        "system_instructions": load("system"),
        "capabilities": [
            {"name": c.name, "description": c.description, "instructions": c.instructions}
            for c in all_capabilities()
        ],
    }


@router.get("/inventory/{category}")
def items(
    category: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    q: str = Query(default="", max_length=200),
) -> dict[str, Any]:
    if category == "model_calls":
        rows = [row for row in _calls() if q.casefold() in json.dumps(row).casefold()]
        return {
            "items": rows[offset : offset + limit],
            "total": len(rows),
            "offset": offset,
            "limit": limit,
        }
    if category not in TABLES:
        raise HTTPException(404, "Unknown artifact category")
    model, _ = TABLES[category]
    query = _owned(model, get_settings().default_user_id)
    if q.strip():
        fields = (
            "text",
            "content",
            "key",
            "doc_id",
            "recipient",
            "excerpt",
            "session_id",
            "thread_id",
        )
        clauses = [
            col(getattr(model, name)).contains(q.strip(), autoescape=True)
            for name in fields
            if hasattr(model, name)
        ]
        if not clauses:
            return {"items": [], "total": 0, "offset": offset, "limit": limit}
        query = query.where(or_(*clauses))
    with session() as s:
        total = s.exec(select(func.count()).select_from(query.subquery())).one()
        rows = s.exec(query.order_by(col(model.id).desc()).offset(offset).limit(limit)).all()
        data = [
            row.model_dump(mode="json", exclude={"user_id", "idempotency_key", "run_messages_json"})
            for row in rows
        ]
    return {"items": data, "total": total, "offset": offset, "limit": limit}


DOC_VIEW_CHARS = 20_000


def _doc_files() -> dict[str, Any]:
    folder = get_settings().docs_path
    return {p.name: p for p in sorted(folder.iterdir()) if p.is_file()} if folder.is_dir() else {}


@router.get("/docs")
def docs() -> list[dict[str, Any]]:
    """The documents in the workspace docs folder."""
    return [
        {"name": name, "bytes": p.stat().st_size, "modified": p.stat().st_mtime}
        for name, p in _doc_files().items()
    ]


@router.get("/docs/{name}")
def doc(name: str) -> dict[str, Any]:
    """One document's start (bounded); only names listed by `/docs` resolve."""
    path = _doc_files().get(name)
    if path is None:
        raise HTTPException(404, "Unknown document")
    with path.open(encoding="utf-8", errors="replace") as stream:
        text = stream.read(DOC_VIEW_CHARS + 1)
    return {"name": name, "text": text[:DOC_VIEW_CHARS], "truncated": len(text) > DOC_VIEW_CHARS}


@router.get("/status")
def status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "token_count_mode": settings.count_method,
        "count_required_before_inference": True,
        "max_input_tokens_exclusive": settings.max_input_tokens,
        "forget_mode": settings.memory_forget_mode,
        "limitations": [
            "Slack retries each accepted event three times; exhausted failures are logged.",
            "No background drain of pending Slack deliveries after restart or exhausted retries.",
            "A crash between generation and saving a receipt can rerun generation.",
            "A crash after Slack accepts a post and before receipt commit can duplicate a reply.",
            "Conversation locks are process-local; run one Slack worker per state database.",
            "Separate HTTP and Slack processes do not share conversation locks.",
            "Receipts without a remaining reply reference are omitted from the owner inventory.",
        ],
    }
