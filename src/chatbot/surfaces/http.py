"""HTTP surface (FastAPI): the core over HTTP, plus thread listing for the web UI.

    uv run chatbot serve            # http://127.0.0.1:8000  (POST /send, web UI at /)

Thin adapter: every message goes through `core.send_async`. Single user (no auth); the
user id defaults to DEFAULT_USER_ID.
"""

import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import col, func, select

from chatbot.config import REPO_ROOT, get_settings
from chatbot.core import SLACK_THREAD_ID, send_async
from chatbot.memory import messages as store
from chatbot.memory.db import session
from chatbot.memory.models import Message, Thread

WEB_DIST = REPO_ROOT / "web" / "dist"


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    thread_id: str = Field(min_length=1, max_length=100)
    user_id: str | None = None


class SendOut(BaseModel):
    reply: str
    thread_id: str


class ThreadOut(BaseModel):
    id: str
    kind: str
    title: str
    updated_at: str


class MessageOut(BaseModel):
    role: str
    text: str
    created_at: str


app = FastAPI(title="chatbot", version="0.1.0")


@app.post("/send", response_model=SendOut)
async def send(body: SendIn) -> SendOut:
    user_id = body.user_id or get_settings().default_user_id
    reply = await send_async(user_id, body.thread_id, body.text)
    return SendOut(reply=reply, thread_id=body.thread_id)


@app.post("/threads", response_model=ThreadOut)
def new_thread() -> ThreadOut:
    """A fresh session id (the thread row is created on its first message)."""
    tid = f"s-{uuid.uuid4().hex[:8]}"
    return ThreadOut(id=tid, kind="session", title="New chat", updated_at="")


@app.get("/threads", response_model=list[ThreadOut])
def threads() -> list[ThreadOut]:
    user_id = get_settings().default_user_id
    with session() as s:
        rows = s.exec(select(Thread).where(Thread.user_id == user_id)).all()
        out = []
        for t in rows:
            first = s.exec(
                select(Message.text)
                .where(Message.thread_id == t.id, Message.role == "user")
                .order_by(col(Message.id))
                .limit(1)
            ).first()
            last = s.exec(
                select(func.max(Message.created_at)).where(Message.thread_id == t.id)
            ).one()
            title = "Slack DM" if t.id == SLACK_THREAD_ID else (first or "New chat")[:60]
            out.append(
                ThreadOut(id=t.id, kind=str(t.kind.value), title=title, updated_at=str(last or ""))
            )
    return sorted(out, key=lambda t: t.updated_at, reverse=True)


@app.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
def thread_messages(thread_id: str, limit: int = 100) -> list[MessageOut]:
    rows = store.thread_messages(thread_id)[-max(1, min(limit, 500)) :]
    if rows and rows[0].user_id != get_settings().default_user_id:
        raise HTTPException(404)
    return [
        MessageOut(role=str(m.role.value), text=m.text, created_at=str(m.created_at)) for m in rows
    ]


@app.get("/health")
def health() -> dict:
    s = get_settings()
    return {
        "ok": True,
        "model": f"{s.llm_provider}:{s.llm_model}",
        "max_input_tokens": s.max_input_tokens,
    }


if WEB_DIST.is_dir():  # built web UI (cd web && npm run build)
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        target = WEB_DIST / path
        return FileResponse(target if path and target.is_file() else WEB_DIST / "index.html")
