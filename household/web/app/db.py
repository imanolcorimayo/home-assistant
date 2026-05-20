"""Single Postgres connection point for the web app — the lib/db.php analogue.

One asyncpg pool, opened on startup and reused for every request. Nothing
else in the app opens a connection. asyncpg lets us write plain SQL and get
back dict-like rows (Record), so no ORM sits between us and the query.

Note: asyncpg wants a plain `postgresql://` DSN — NOT the server's
`postgresql+asyncpg://` form (that prefix is a SQLAlchemy thing).
"""

import os

import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]

_pool: asyncpg.Pool | None = None


async def open_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


async def fetch(query: str, *args) -> list[asyncpg.Record]:
    """Run a SELECT, return all rows."""
    async with _pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args) -> asyncpg.Record | None:
    """Run a SELECT, return the first row (or None)."""
    async with _pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def execute(query: str, *args) -> str:
    """Run an INSERT/UPDATE/DELETE, return the status tag (e.g. 'INSERT 0 1')."""
    async with _pool.acquire() as conn:
        return await conn.execute(query, *args)
