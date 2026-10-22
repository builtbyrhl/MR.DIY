from __future__ import annotations

from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateColumn

from bot.config import get_settings

_engine = None
_session_factory = None

_UPLOAD_ITEM_NEW_COLUMNS = (
    ("file_id", String(256)),
    ("publication_status", String(32)),
    ("published_message_id", BigInteger()),
)

_LECTURE_SOURCE_UPLOAD_INDEX = "uq_lectures_source_upload_item"


def ensure_upload_item_columns(connection) -> None:
    """Add publishing columns to an existing upload_items table.

    create_all() creates missing tables but never alters existing ones. This
    migration is idempotent and safe for both PostgreSQL and SQLite.
    """
    inspector = inspect(connection)
    if not inspector.has_table("upload_items"):
        return

    existing = {column["name"] for column in inspector.get_columns("upload_items")}
    table = connection.dialect.identifier_preparer.quote("upload_items")
    if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
    for name, column_type in _UPLOAD_ITEM_NEW_COLUMNS:
        if name in existing:
            continue
        definition = str(
            CreateColumn(Column(name, column_type, nullable=True)).compile(
                dialect=connection.dialect
            )
        )
        connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {if_not_exists}{definition}")
        )


def ensure_lecture_columns(connection) -> None:
    """Add the published-upload link to an existing lectures table.

    Existing legacy lectures keep a NULL link, so they continue to play from
    CHANNEL_ID. The unique index makes repeated projection/backfill safe.
    """
    inspector = inspect(connection)
    if not inspector.has_table("lectures"):
        return

    existing = {column["name"] for column in inspector.get_columns("lectures")}
    table = connection.dialect.identifier_preparer.quote("lectures")
    if "source_upload_item_id" not in existing:
        if_not_exists = (
            "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
        )
        column = Column(
            "source_upload_item_id",
            Integer,
            ForeignKey("upload_items.id", ondelete="SET NULL"),
            nullable=True,
        )
        definition = str(
            CreateColumn(column).compile(dialect=connection.dialect)
        )
        connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {if_not_exists}{definition}")
        )

    index = connection.dialect.identifier_preparer.quote(
        _LECTURE_SOURCE_UPLOAD_INDEX
    )
    connection.execute(
        text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index} "
            f"ON {table} (source_upload_item_id)"
        )
    )


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(s.database_url, echo=False, pool_pre_ping=True)
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _session_factory


async def init_db():
    from bot.db.models import Base
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(ensure_upload_item_columns)
        await conn.run_sync(ensure_lecture_columns)
