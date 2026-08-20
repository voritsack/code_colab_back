"""Async SQLAlchemy engine and session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings
from .models import Base


def _engine_kwargs() -> dict:
    """Extra engine options that only some dialects accept.

    MySQL/MariaDB and PostgreSQL default to REPEATABLE READ, which means a
    long-lived transaction keeps reading the snapshot it started with. The
    WebSocket handler holds one session for the life of a connection, so an
    idle host would stop seeing rows other connections had committed - a
    newcomer in the lobby, for instance. READ COMMITTED is the correct
    isolation for this workload. SQLite does not accept the setting.
    """
    url = settings.database_url
    if url.startswith(("mysql", "mariadb", "postgresql")):
        return {"isolation_level": "READ COMMITTED"}
    return {}


engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
    **_engine_kwargs(),
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# Columns added after a table shipped. `create_all` only creates what is
# missing wholesale, so an existing deployment would keep the old table and
# every query naming the new column would fail. Until there is a real
# migration story, additive columns go here: each is checked before it is
# added, so a boot on an up-to-date database does nothing.
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("attachments", "path", "VARCHAR(400) NOT NULL DEFAULT ''"),
)


def _add_missing_columns(conn) -> None:
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    for table, column, ddl in ADDED_COLUMNS:
        if table not in tables:
            continue  # create_all just made it, with the column already on it
        if column in {c["name"] for c in inspector.get_columns(table)}:
            continue
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def init_models() -> None:
    """Create any missing tables, and any column added to an existing one.

    Deliberately simple: this project has no migration story yet, so the
    schema is created on boot. Swap in Alembic before you start changing
    columns - as opposed to adding them - on a database that already holds
    real data.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
