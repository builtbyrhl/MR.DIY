import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sqlalchemy import func, select

from bot.db.ingestion_repo import approve_batch, skip_item
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
        video=SimpleNamespace(
            file_name=None,
            file_id="handler-file-id",
            file_unique_id="handler-video",
        ),
        caption="Anatomy A Lecture 40",
    )

    await ingest_handler.ingest_message(message, session)

    item = await session.scalar(select(UploadItem))
    assert item is not None
    assert item.media_type == "video"
    assert item.file_id == "handler-file-id"
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
        document=SimpleNamespace(
            file_name="Anatomy A Lecture 41.pdf",
            file_id="handler-doc-file-id",
            file_unique_id="handler-doc",
        ),
    )

    await ingest_handler.ingest_message(message, session)

    item = await session.scalar(select(UploadItem))
    assert item is not None
    assert item.media_type == "document"
    assert item.file_id == "handler-doc-file-id"
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


async def test_edited_caption_reclassifies_existing_upload(
    monkeypatch, session, seeded_course
):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-1001,
        message_id=43,
        video=SimpleNamespace(
            file_name=None,
            file_id="edit-file-id",
            file_unique_id="edited-video",
        ),
        caption="Anatomy A Lecture 43",
    )
    await ingest_handler.ingest_message(message, session)
    item = await session.scalar(select(UploadItem))
    assert item is not None
    original_caption = item.raw_caption

    message.caption = "Anatomy A Lecture 44"
    await ingest_handler.ingest_edited_message(message, session)
    await session.refresh(item)

    assert item.raw_caption == "Anatomy A Lecture 44"
    assert item.original_caption == original_caption
    assert item.lecture_number == 44
    assert item.status == "classified"


async def test_edited_caption_is_ignored_after_approval(
    monkeypatch, session, seeded_course
):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-1001,
        message_id=44,
        video=SimpleNamespace(
            file_name=None,
            file_id="approved-edit-file",
            file_unique_id="approved-edit-video",
        ),
        caption="Anatomy A Lecture 44",
    )
    await ingest_handler.ingest_message(message, session)
    item = await session.scalar(select(UploadItem))
    ok, reason = await approve_batch(session, item.batch_id, 123)
    assert ok is True, reason

    message.caption = "Anatomy A Lecture 99"
    await ingest_handler.ingest_edited_message(message, session)
    await session.refresh(item)

    assert item.raw_caption == "Anatomy A Lecture 44"
    assert item.lecture_number == 44


async def test_edited_caption_does_not_revive_skipped_upload(
    monkeypatch, session, seeded_course
):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-1001,
        message_id=45,
        video=SimpleNamespace(
            file_name=None,
            file_id="skipped-edit-file",
            file_unique_id="skipped-edit-video",
        ),
        caption="Anatomy A Lecture 45",
    )
    await ingest_handler.ingest_message(message, session)
    item = await session.scalar(select(UploadItem))
    assert item is not None
    original_caption = item.raw_caption
    original_number = item.lecture_number
    assert await skip_item(session, item.id) is True

    message.caption = "Anatomy A Lecture 99"
    await ingest_handler.ingest_edited_message(message, session)
    await session.refresh(item)

    assert item.status == "skipped"
    assert item.raw_caption == original_caption
    assert item.lecture_number == original_number


async def test_edited_caption_does_not_create_missing_upload(
    monkeypatch, session, seeded_course
):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-1001,
        message_id=77,
        video=SimpleNamespace(
            file_name=None,
            file_id="missing-edit-file",
            file_unique_id="missing-edit-video",
        ),
        caption="Anatomy A Lecture 77",
    )

    await ingest_handler.ingest_edited_message(message, session)

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
        video=SimpleNamespace(
            file_name=None,
            file_id="failing-file-id",
            file_unique_id="failing-upload",
        ),
        caption="Anatomy A Lecture 99",
    )

    with caplog.at_level(logging.ERROR, logger="bot.handlers.ingest"):
        with pytest.raises(RuntimeError):
            await ingest_handler.ingest_message(message, session)

    assert "Failed to store upload chat=-1001 message=99" in caplog.text
    assert "simulated database failure" in caplog.text


@pytest.mark.parametrize(
    ("video", "message_id"),
    [
        (SimpleNamespace(file_name=None, file_id=None, file_unique_id="only-unique"), 50),
        (SimpleNamespace(file_name=None, file_id="only-file-id", file_unique_id=None), 51),
        (SimpleNamespace(file_name=None, file_id="", file_unique_id=""), 52),
        (SimpleNamespace(file_name=None, file_id="file-53", file_unique_id="unique-53"), 0),
    ],
)
async def test_handler_rejects_malformed_uploads(
    monkeypatch, session, video, message_id, caplog
):
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-1001,
        message_id=message_id,
        video=video,
        caption="Anatomy A Lecture 50",
    )

    with caplog.at_level(logging.WARNING, logger="bot.handlers.ingest"):
        await ingest_handler.ingest_message(message, session)

    count = await session.scalar(select(func.count(UploadItem.id)))
    assert count == 0
    assert "Ignored malformed upload" in caplog.text


async def test_edited_handler_ignores_malformed_media(
    monkeypatch, session, seeded_course
):
    seeded_course
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id="-1001"),
    )
    message = _fake_message(
        chat_id=-1001,
        message_id=54,
        video=SimpleNamespace(
            file_name=None,
            file_id="valid-file-54",
            file_unique_id="valid-unique-54",
        ),
        caption="Anatomy A Lecture 54",
    )
    await ingest_handler.ingest_message(message, session)
    item = await session.scalar(select(UploadItem))
    assert item is not None
    original_caption = item.raw_caption

    message.caption = "Anatomy A Lecture 99"
    message.video = SimpleNamespace(
        file_name=None, file_id=None, file_unique_id=None
    )
    await ingest_handler.ingest_edited_message(message, session)
    await session.refresh(item)

    assert item.raw_caption == original_caption
    assert item.lecture_number == 54
