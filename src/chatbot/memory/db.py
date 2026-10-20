"""SQLite engine and schema setup."""

from functools import cache

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from chatbot.config import get_settings
from chatbot.memory import models  # noqa: F401  (registers tables)


@cache
def get_engine() -> Engine:
    path = get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _record) -> None:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    SQLModel.metadata.create_all(engine)
    return engine


def session() -> Session:
    return Session(get_engine(), expire_on_commit=False)
