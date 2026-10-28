from __future__ import annotations

import logging

from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Integer, String, Text, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateColumn

from bot.config import get_settings

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None

_UPLOAD_ITEM_NEW_COLUMNS = (
    ("file_id", String(256)),
    ("publication_status", String(32)),
    ("published_message_id", BigInteger()),
    ("teacher_id", Integer()),
    ("teacher_source", String(16)),
    ("teacher_review_reason", Text()),
    ("original_caption", Text()),
    ("topic_source", String(16)),
    ("topic_review_reason", Text()),
    ("institution_id", Integer()),
)

_UPLOAD_BATCH_NEW_COLUMNS = (
    ("name", String(128)),
)

_LECTURE_SOURCE_UPLOAD_INDEX = "uq_lectures_source_upload_item"
_LECTURE_TEACHER_INDEX = "ix_lectures_subject_teacher_order"

_USER_ACCESS_NEW_COLUMNS = (
    ("access_status", String(16)),
    ("access_requested_at", DateTime()),
    ("access_decided_at", DateTime()),
    ("access_decided_by", BigInteger()),
    ("access_expires_at", DateTime()),
    ("streak", Integer()),
    ("last_study_date", Date()),
)

_USER_ACCESS_INDEX = "ix_users_access_status"

_OPEN_BATCH_INDEX = "uq_upload_batches_open_source_date"


def ensure_open_batch_unique_index(connection) -> None:
    """Allow only one open upload batch per source chat and date.

    Older databases may already contain duplicate open batches. In that case
    the index is skipped and a warning is logged instead of deleting or
    merging existing rows.
    """
    inspector = inspect(connection)
    if not inspector.has_table("upload_batches"):
        return

    table = connection.dialect.identifier_preparer.quote("upload_batches")
    duplicates = connection.execute(
        text(
            f"SELECT source_chat_id, batch_date, COUNT(*) AS total "
            f"FROM {table} WHERE status = 'open' "
            "GROUP BY source_chat_id, batch_date HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicates is not None:
        logger.warning(
            "Skipping %s: duplicate open upload batches exist for "
            "source_chat_id=%s batch_date=%s",
            _OPEN_BATCH_INDEX,
            duplicates.source_chat_id,
            duplicates.batch_date,
        )
        return

    index = connection.dialect.identifier_preparer.quote(_OPEN_BATCH_INDEX)
    connection.execute(
        text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index} "
            f"ON {table} (source_chat_id, batch_date) WHERE status = 'open'"
        )
    )


def ensure_upload_batch_columns(connection) -> None:
    """Add batch metadata columns to an existing upload_batches table."""
    inspector = inspect(connection)
    if not inspector.has_table("upload_batches"):
        return

    existing = {column["name"] for column in inspector.get_columns("upload_batches")}
    table = connection.dialect.identifier_preparer.quote("upload_batches")
    if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
    for name, column_type in _UPLOAD_BATCH_NEW_COLUMNS:
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
    """Add published-upload and teacher links to an existing lectures table.

    Existing legacy lectures keep a NULL link, so they continue to play from
    CHANNEL_ID. The unique index makes repeated projection/backfill safe.
    Existing lectures also keep teacher_id NULL and stay visible in the
    "Teacher not specified" bucket.
    """
    inspector = inspect(connection)
    if not inspector.has_table("lectures"):
        return

    existing = {column["name"] for column in inspector.get_columns("lectures")}
    table = connection.dialect.identifier_preparer.quote("lectures")
    if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
    if "institution_id" not in existing:
        definition = str(
            CreateColumn(Column("institution_id", Integer, nullable=True)).compile(
                dialect=connection.dialect
            )
        )
        connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {if_not_exists}{definition}")
        )
        idx = connection.dialect.identifier_preparer.quote("ix_lectures_institution")
        connection.execute(
            text(f"CREATE INDEX IF NOT EXISTS {idx} ON {table} (institution_id)")
        )
    if "source_upload_item_id" not in existing:
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
    if "teacher_id" not in existing:
        # Legacy databases cannot add a foreign key with ALTER TABLE, so the
        # application validates subject/teacher consistency instead.
        definition = str(
            CreateColumn(Column("teacher_id", Integer, nullable=True)).compile(
                dialect=connection.dialect
            )
        )
        connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {if_not_exists}{definition}")
        )

    index = connection.dialect.identifier_preparer.quote(
        _LECTURE_SOURCE_UPLOAD_INDEX
    )
    duplicate_source = connection.execute(
        text(
            f"SELECT source_upload_item_id, COUNT(*) AS total "
            f"FROM {table} WHERE source_upload_item_id IS NOT NULL "
            "GROUP BY source_upload_item_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_source is not None:
        logger.warning(
            "Skipping %s: duplicate lecture rows reference upload item %s. "
            "Repair the duplicates, then restart to create the index.",
            _LECTURE_SOURCE_UPLOAD_INDEX,
            duplicate_source.source_upload_item_id,
        )
    else:
        connection.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index} "
                f"ON {table} (source_upload_item_id)"
            )
        )
    teacher_index = connection.dialect.identifier_preparer.quote(
        _LECTURE_TEACHER_INDEX
    )
    order_column = connection.dialect.identifier_preparer.quote("order")
    connection.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {teacher_index} "
            f"ON {table} (subject_id, teacher_id, {order_column})"
        )
    )


def ensure_course_columns(connection) -> None:
    """Add institution_id to an existing courses table.

    Like lecture teacher_id, the column is added without a foreign key so the
    migration works on legacy databases; the application validates institution
    consistency instead.
    """
    inspector = inspect(connection)
    if not inspector.has_table("courses"):
        return
    existing = {column["name"] for column in inspector.get_columns("courses")}
    table = connection.dialect.identifier_preparer.quote("courses")
    if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
    if "institution_id" not in existing:
        definition = str(
            CreateColumn(Column("institution_id", Integer, nullable=True)).compile(
                dialect=connection.dialect
            )
        )
        connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {if_not_exists}{definition}")
        )
    idx = connection.dialect.identifier_preparer.quote("ix_courses_institution")
    connection.execute(
        text(f"CREATE INDEX IF NOT EXISTS {idx} ON {table} (institution_id)")
    )


def ensure_institution_backfill(connection) -> None:
    """Create default institution and backfill existing lectures/items/courses."""
    from bot.db.models import Course, Institution, Lecture, UploadItem

    inspector = inspect(connection)
    if not inspector.has_table("institutions"):
        return

    # Create default institution if none exists
    res = connection.execute(text("SELECT id FROM institutions ORDER BY id LIMIT 1"))
    row = res.first()
    if row is None:
        connection.execute(
            text(
                "INSERT INTO institutions (name, slug) VALUES (:name, :slug)"
            ),
            {"name": "Default Institution", "slug": "default-institution"},
        )
        res = connection.execute(text("SELECT id FROM institutions ORDER BY id LIMIT 1"))
        row = res.first()
    if row is None:
        return
    inst_id = row[0]

    # Backfill lectures
    if inspector.has_table("lectures"):
        existing_lect_cols = {c["name"] for c in inspector.get_columns("lectures")}
        if "institution_id" in existing_lect_cols:
            connection.execute(
                text("UPDATE lectures SET institution_id = :inst_id WHERE institution_id IS NULL"),
                {"inst_id": inst_id},
            )
    # Backfill courses
    if inspector.has_table("courses"):
        existing_course_cols = {c["name"] for c in inspector.get_columns("courses")}
        if "institution_id" in existing_course_cols:
            connection.execute(
                text("UPDATE courses SET institution_id = :inst_id WHERE institution_id IS NULL"),
                {"inst_id": inst_id},
            )
    # Backfill upload items
    if inspector.has_table("upload_items"):
        existing_item_cols = {c["name"] for c in inspector.get_columns("upload_items")}
        if "institution_id" in existing_item_cols:
            connection.execute(
                text("UPDATE upload_items SET institution_id = :inst_id WHERE institution_id IS NULL"),
                {"inst_id": inst_id},
            )


def ensure_user_access_columns(connection) -> None:
    """Add persistent student access state to an existing users table.

    New users default to pending in the ORM. Existing users are backfilled to
    approved so deploying this migration does not lock out current students.
    """
    inspector = inspect(connection)
    if not inspector.has_table("users"):
        return

    existing = {column["name"] for column in inspector.get_columns("users")}
    table = connection.dialect.identifier_preparer.quote("users")
    if_not_exists = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
    for name, column_type in _USER_ACCESS_NEW_COLUMNS:
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

    if "access_status" not in existing:
        # Only legacy rows touched by this one-time migration are grandfathered.
        # Rows with no state later are treated as pending by the access policy.
        connection.execute(
            text(
                f"UPDATE {table} SET access_status = 'approved' "
                "WHERE access_status IS NULL"
            )
        )
    index = connection.dialect.identifier_preparer.quote(_USER_ACCESS_INDEX)
    connection.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {index} "
            f"ON {table} (access_status)"
        )
    )


def _engine_options(database_url: str) -> dict:
    """Return create_async_engine options that are safe for this database.

    ``pool_pre_ping`` is deliberately disabled. With async SQLAlchemy the
    pre-ping health check can run outside the greenlet context of the asyncpg
    driver and raise ``MissingGreenlet`` while a pooled connection is being
    checked. ``pool_recycle`` replaces idle connections instead, which keeps
    stale-connection failures bounded without that code path.
    """
    options = {"echo": False, "pool_pre_ping": False}
    if database_url.startswith("sqlite"):
        return options
    options.update(
        {
            "pool_recycle": 1800,
            "pool_size": 5,
            "max_overflow": 5,
            "pool_timeout": 10,
            "connect_args": {"timeout": 10},
        }
    )
    return options


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        try:
            _engine = create_async_engine(s.database_url, **_engine_options(s.database_url))
        except Exception as exc:
            logger.error(
                "Failed to create database engine (%s). Check DATABASE_URL syntax.",
                type(exc).__name__,
            )
            raise
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)
    return _session_factory


_CONNECTION_ERROR_NAMES = {
    "OperationalError",
    "InterfaceError",
    "TimeoutError",
    "ConnectionError",
    "ConnectionRefusedError",
    "OSError",
}


def _is_connection_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in _CONNECTION_ERROR_NAMES:
        return True
    # asyncpg and SQLAlchemy wrap connection faults in several subtypes.
    return isinstance(exc, (OSError,)) or "connect" in name.lower()


async def _verify_connection(engine) -> None:
    """Fail fast with a clear message when the database is unreachable."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error(
            "Database connection check failed (%s). Verify DATABASE_URL, host, "
            "port, and network access. Credentials are not logged.",
            type(exc).__name__,
        )
        raise
    logger.info("Database connection verified")


async def init_db():
    from bot.db.models import Base

    engine = get_engine()
    await _verify_connection(engine)

    steps = (
        ("create tables", Base.metadata.create_all),
        ("upload batch columns", ensure_upload_batch_columns),
        ("upload item columns", ensure_upload_item_columns),
        ("lecture columns", ensure_lecture_columns),
        ("course columns", ensure_course_columns),
        ("user access columns", ensure_user_access_columns),
        ("open batch unique index", ensure_open_batch_unique_index),
        ("institution backfill", ensure_institution_backfill),
    )
    async with engine.begin() as conn:
        for name, step in steps:
            try:
                await conn.run_sync(step)
            except Exception as exc:
                if _is_connection_error(exc):
                    logger.error(
                        "Database startup failed at migration step '%s': "
                        "connection problem (%s).",
                        name,
                        type(exc).__name__,
                    )
                else:
                    logger.error(
                        "Database startup failed at migration step '%s': "
                        "migration error (%s).",
                        name,
                        type(exc).__name__,
                    )
                raise
            logger.info("Database startup step complete: %s", name)
