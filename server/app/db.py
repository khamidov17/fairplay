"""Async engine/session setup. Works on PostgreSQL (asyncpg) and SQLite (aiosqlite)."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import Settings


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, settings: Settings):
        url = settings.database_url
        self.is_sqlite = url.startswith("sqlite")
        if self.is_sqlite:
            path = url.split("///", 1)[-1]
            if path and path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            self.engine: AsyncEngine = create_async_engine(
                url, echo=settings.sql_echo, connect_args={"timeout": 30})

            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _rec):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        else:
            self.engine = create_async_engine(
                url, echo=settings.sql_echo,
                pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
                pool_timeout=60, pool_recycle=1800, pool_pre_ping=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def create_all(self) -> None:
        from . import models  # noqa: F401  (register tables)
        async with self.engine.begin() as conn:
            if not self.is_sqlite:
                # Several workers start at once: serialize DDL.
                await conn.execute(text("SELECT pg_advisory_xact_lock(724001)"))
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)

    async def dispose(self) -> None:
        await self.engine.dispose()


def _add_missing_columns(conn) -> None:
    """Tiny forward-only migration: create_all never alters existing tables, so add any
    model column (and index) an older database lacks. New columns carry a server_default."""
    from sqlalchemy import inspect
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=conn.dialect)}"
            if col.server_default is not None:
                arg = col.server_default.arg
                if isinstance(arg, str):
                    default = "'" + arg.replace("'", "''") + "'"
                else:
                    default = str(arg.compile(dialect=conn.dialect))
                ddl += f" DEFAULT {default}"
                if not col.nullable:
                    ddl += " NOT NULL"
            conn.execute(text(ddl))
        have_ix = {ix["name"] for ix in insp.get_indexes(table.name)}
        for ix in table.indexes:
            if ix.name not in have_ix:
                ix.create(conn, checkfirst=True)


_db: Database | None = None


def set_db(db: Database | None) -> None:
    global _db
    _db = db


def get_database() -> Database:
    if _db is None:
        raise RuntimeError("database not initialised")
    return _db


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_database().sessionmaker() as session:
        yield session
