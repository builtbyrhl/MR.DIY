import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from bot.db import session as db_session


def _columns(connection):
    return {column["name"] for column in inspect(connection).get_columns("upload_items")}


async def test_startup_migration_adds_publishing_columns_without_data_loss(
    tmp_path, monkeypatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/old-schema.db")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE upload_items (
                    id INTEGER PRIMARY KEY,
                    batch_id INTEGER NOT NULL,
                    source_chat_id BIGINT NOT NULL,
                    source_message_id BIGINT NOT NULL,
                    media_type VARCHAR(16) NOT NULL,
                    file_unique_id VARCHAR(128),
                    status VARCHAR(32) NOT NULL
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO upload_items
                    (id, batch_id, source_chat_id, source_message_id,
                     media_type, file_unique_id, status)
                VALUES
                    (1, 10, -1001, 55, 'video', 'existing-unique-id', 'classified')
                """
            )
        )

    monkeypatch.setattr(db_session, "get_engine", lambda: engine)
    await db_session.init_db()
    await db_session.init_db()

    async with engine.connect() as connection:
        columns = await connection.run_sync(_columns)
        row = (
            await connection.execute(
                text(
                    "SELECT id, file_unique_id, status FROM upload_items WHERE id = 1"
                )
            )
        ).one()
        has_topics = await connection.run_sync(
            lambda conn: inspect(conn).has_table("topics")
        )
        has_upload_topics = await connection.run_sync(
            lambda conn: inspect(conn).has_table("upload_item_topics")
        )
        has_lecture_topics = await connection.run_sync(
            lambda conn: inspect(conn).has_table("lecture_topics")
        )
        has_teacher_aliases = await connection.run_sync(
            lambda conn: inspect(conn).has_table("teacher_aliases")
        )
        has_admin_users = await connection.run_sync(
            lambda conn: inspect(conn).has_table("admin_users")
        )
        has_student_media = await connection.run_sync(
            lambda conn: inspect(conn).has_table("student_media_messages")
        )
        has_rollback_operations = await connection.run_sync(
            lambda conn: inspect(conn).has_table("rollback_operations")
        )
        has_rollback_items = await connection.run_sync(
            lambda conn: inspect(conn).has_table("rollback_items")
        )

    assert {
        "file_id",
        "publication_status",
        "published_message_id",
        "published_chat_id",
        "published_topic_id",
        "teacher_id",
        "teacher_source",
        "teacher_review_reason",
        "original_caption",
        "topic_source",
        "topic_review_reason",
    } <= columns
    assert has_topics is True
    assert has_upload_topics is True
    assert has_lecture_topics is True
    assert has_teacher_aliases is True
    assert has_admin_users is True
    assert has_student_media is True
    assert has_rollback_operations is True
    assert has_rollback_items is True
    assert row.id == 1
    assert row.file_unique_id == "existing-unique-id"
    assert row.status == "classified"

    await engine.dispose()


async def test_lecture_migration_adds_withdrawal_columns_and_backfills(
    tmp_path,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/legacy-lectures.db")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE lectures (
                    id INTEGER PRIMARY KEY,
                    subject_id INTEGER NOT NULL,
                    title VARCHAR(256) NOT NULL
                )
                """
            )
        )
        await connection.execute(
            text(
                "INSERT INTO lectures (id, subject_id, title) "
                "VALUES (1, 2, 'Legacy lecture')"
            )
        )
        await connection.run_sync(db_session.ensure_lecture_columns)
        await connection.run_sync(db_session.ensure_lecture_columns)
        columns = await connection.run_sync(
            lambda conn: {
                column["name"]
                for column in inspect(conn).get_columns("lectures")
            }
        )
        row = (
            await connection.execute(
                text(
                    "SELECT title, is_withdrawn FROM lectures WHERE id = 1"
                )
            )
        ).one()

    assert {"is_withdrawn", "withdrawal_status", "withdrawn_at"} <= columns
    assert row.title == "Legacy lecture"
    assert row.is_withdrawn in (False, 0)

    await engine.dispose()


async def test_startup_migration_preserves_existing_user_access(
    tmp_path, monkeypatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/legacy-users.db")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE users (
                    id BIGINT PRIMARY KEY,
                    username VARCHAR(64),
                    full_name VARCHAR(128),
                    is_premium BOOLEAN NOT NULL DEFAULT 0,
                    premium_until DATETIME,
                    is_admin BOOLEAN NOT NULL DEFAULT 0,
                    streak INTEGER NOT NULL DEFAULT 0,
                    last_active DATETIME,
                    created_at DATETIME
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO users (id, username, full_name, is_admin)
                VALUES (999, 'existing_student', 'Existing Student', 0)
                """
            )
        )

    monkeypatch.setattr(db_session, "get_engine", lambda: engine)
    await db_session.init_db()
    await db_session.init_db()

    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda conn: {
                column["name"]
                for column in inspect(conn).get_columns("users")
            }
        )
        indexes = await connection.run_sync(
            lambda conn: {
                index["name"]
                for index in inspect(conn).get_indexes("users")
            }
        )
        row = (
            await connection.execute(
                text(
                    "SELECT id, access_status, access_decided_at "
                    "FROM users WHERE id = 999"
                )
            )
        ).one()

    assert {
        "access_status",
        "access_requested_at",
        "access_decided_at",
        "access_decided_by",
        "access_expires_at",
    } <= columns
    assert "ix_users_access_status" in indexes
    assert row.id == 999
    assert row.access_status == "approved"
    assert row.access_decided_at is None

    await engine.dispose()


async def test_open_batch_migration_adds_unique_index(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/legacy-batches.db")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE upload_batches (
                    id INTEGER PRIMARY KEY,
                    source_chat_id BIGINT NOT NULL,
                    batch_date DATE NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    approved_by BIGINT,
                    approved_at DATETIME,
                    created_at DATETIME
                )
                """
            )
        )

    monkeypatch.setattr(db_session, "get_engine", lambda: engine)
    await db_session.init_db()
    await db_session.init_db()

    async with engine.connect() as connection:
        indexes = await connection.run_sync(
            lambda conn: {
                index["name"]
                for index in inspect(conn).get_indexes("upload_batches")
            }
        )

    assert "uq_upload_batches_open_source_date" in indexes

    await engine.dispose()


async def test_open_batch_migration_skips_duplicate_open_batches(
    tmp_path, monkeypatch, caplog
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/duplicate-batches.db")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE upload_batches (
                    id INTEGER PRIMARY KEY,
                    source_chat_id BIGINT NOT NULL,
                    batch_date DATE NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    approved_by BIGINT,
                    approved_at DATETIME,
                    created_at DATETIME
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO upload_batches
                    (id, source_chat_id, batch_date, status)
                VALUES
                    (1, -1001, '2026-01-15', 'open'),
                    (2, -1001, '2026-01-15', 'open')
                """
            )
        )

    monkeypatch.setattr(db_session, "get_engine", lambda: engine)
    with caplog.at_level(logging.WARNING):
        await db_session.init_db()

    async with engine.connect() as connection:
        indexes = await connection.run_sync(
            lambda conn: {
                index["name"]
                for index in inspect(conn).get_indexes("upload_batches")
            }
        )
        count = (
            await connection.execute(
                text("SELECT COUNT(*) FROM upload_batches")
            )
        ).scalar_one()

    assert "uq_upload_batches_open_source_date" not in indexes
    assert count == 2
    assert "duplicate open upload batches" in caplog.text

    await engine.dispose()


async def test_startup_migration_links_legacy_lectures_without_data_loss(
    tmp_path, monkeypatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/legacy-lectures.db")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE lectures (
                    id INTEGER PRIMARY KEY,
                    subject_id INTEGER NOT NULL,
                    title VARCHAR(256) NOT NULL,
                    description TEXT,
                    tags VARCHAR(256),
                    channel_message_id INTEGER,
                    duration_sec INTEGER,
                    is_premium BOOLEAN NOT NULL DEFAULT 0,
                    "order" INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO lectures
                    (id, subject_id, title, channel_message_id, is_premium, "order")
                VALUES
                    (7, 1, 'Legacy Biochemistry Lecture', 456, 0, 7)
                """
            )
        )

    monkeypatch.setattr(db_session, "get_engine", lambda: engine)
    await db_session.init_db()
    await db_session.init_db()

    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda conn: {
                column["name"]
                for column in inspect(conn).get_columns("lectures")
            }
        )
        indexes = await connection.run_sync(
            lambda conn: {
                index["name"]
                for index in inspect(conn).get_indexes("lectures")
            }
        )
        has_teachers = await connection.run_sync(
            lambda conn: inspect(conn).has_table("teachers")
        )
        row = (
            await connection.execute(
                text(
                    "SELECT id, title, channel_message_id, "
                    "source_upload_item_id, teacher_id "
                    "FROM lectures WHERE id = 7"
                )
            )
        ).one()

    assert has_teachers is True
    assert "source_upload_item_id" in columns
    assert "teacher_id" in columns
    assert "uq_lectures_source_upload_item" in indexes
    assert "ix_lectures_subject_teacher_order" in indexes
    assert row.id == 7
    assert row.title == "Legacy Biochemistry Lecture"
    assert row.channel_message_id == 456
    assert row.source_upload_item_id is None
    assert row.teacher_id is None

    await engine.dispose()
