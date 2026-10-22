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

    assert {"file_id", "publication_status", "published_message_id"} <= columns
    assert row.id == 1
    assert row.file_unique_id == "existing-unique-id"
    assert row.status == "classified"

    await engine.dispose()
