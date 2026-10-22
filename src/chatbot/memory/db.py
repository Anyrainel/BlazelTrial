"""SQLite engine and schema setup."""

from functools import cache

import sqlite_vec
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
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    from chatbot.memory import search  # raw-SQL virtual tables live there

    search.create_fts(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Additive schema change for existing databases (no migration framework)."""
    with engine.begin() as conn:
        have = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(message)")}
        if "forgotten" not in have:
            conn.exec_driver_sql(
                "ALTER TABLE message ADD COLUMN forgotten BOOLEAN NOT NULL DEFAULT 0"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_message_forgotten ON message(forgotten)"
            )


def session() -> Session:
    return Session(get_engine(), expire_on_commit=False)
