import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sqlalchemy import func, select

from bot.db.models import UploadItem
from bot.handlers import ingest as ingest_handler


def _fake_message(*, chat_id, message_id, video=None, document=None, caption=None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, username=None),
        message_id=message_id,
        video=video,
        document=document,
        caption=caption,
        forward_origin=None,
        reply=AsyncMock(),
    )


async def test_video_handler_ingests_without_ack(monkeypatch, session, seeded_course):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-1001,
        message_id=40,
        video=SimpleNamespace(file_name=None, file_unique_id="handler-video"),
        caption="Anatomy A Lecture 40",
    )

    await ingest_handler.ingest_message(message, session)

    item = await session.scalar(select(UploadItem))
    assert item is not None
    assert item.media_type == "video"
    assert item.lecture_number == 40
    assert item.status == "classified"
    message.reply.assert_not_awaited()


async def test_document_handler_ingests_without_ack(monkeypatch, session, seeded_course):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-1001,
        message_id=41,
        document=SimpleNamespace(file_name="Anatomy A Lecture 41.pdf", file_unique_id="handler-doc"),
    )

    await ingest_handler.ingest_message(message, session)

    item = await session.scalar(select(UploadItem))
    assert item is not None
    assert item.media_type == "document"
    assert item.lecture_number == 41
    assert item.title == "Anatomy A Lecture 41"
    message.reply.assert_not_awaited()


async def test_handler_ignores_other_chats(monkeypatch, session, seeded_course):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-9999,
        message_id=42,
        video=SimpleNamespace(file_name=None, file_unique_id="other-chat"),
        caption="Anatomy A Lecture 42",
    )

    await ingest_handler.ingest_message(message, session)

    count = await session.scalar(select(func.count(UploadItem.id)))
    assert count == 0


async def test_handler_logs_storage_failure(monkeypatch, session, seeded_course, caplog):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )

    async def fail_ingest(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(ingest_handler, "ingest_media", fail_ingest)
    message = _fake_message(
        chat_id=-1001,
        message_id=99,
        video=SimpleNamespace(file_name=None, file_unique_id="failing-upload"),
        caption="Anatomy A Lecture 99",
    )

    with caplog.at_level(logging.ERROR, logger="bot.handlers.ingest"):
        with pytest.raises(RuntimeError):
            await ingest_handler.ingest_message(message, session)

    assert "Failed to store upload chat=-1001 message=99" in caplog.text
    assert "simulated database failure" in caplog.text
