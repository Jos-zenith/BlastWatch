"""Database engine, sessions and a dialect-aware upsert helper.

SQLite is the default so the project runs anywhere; point BLASTWATCH_DATABASE_URL at
PostgreSQL (postgresql+psycopg://...) for deployment — the upsert helper supports both.
"""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None) -> Engine:
    url = url or config.DATABASE_URL
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind: Engine | None = None) -> None:
    from . import models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(bind or engine)


def upsert(session: Session, model, rows: list[dict], keys: list[str]) -> int:
    """Insert rows, updating non-key columns when a row with the same `keys` exists."""
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise NotImplementedError(f"upsert not supported for {dialect}")

    stmt = insert(model)
    updates = {
        col.name: stmt.excluded[col.name]
        for col in model.__table__.columns
        if col.name not in keys and not col.primary_key and col.name in rows[0]
    }
    if updates:
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=updates)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=keys)
    session.execute(stmt, rows)
    return len(rows)
