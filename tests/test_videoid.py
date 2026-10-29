from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from bot.db.models import UploadBatch, UploadItem
from bot.handlers import ingest as ingest_handler

ADMIN_ID = 123
INVENTORY_CHAT_ID = -1001


def _settings(monkeypatch, chat_id=f"{INVENTORY_CHAT_ID}"):
    monkeypatch.setattr(
        ingest_handler,
        "get_settings",
        lambda: SimpleNamespace(upload_inbox_chat_id=chat_id),
    )


def _reply(*, message_id=101, video=None, document=None):
    return SimpleNamespace(
        message_id=message_id,
        video=video,
        document=document,
    )


def _command(*, user_id=ADMIN_ID, chat_id=INVENTORY_CHAT_ID, message_id=10, reply=None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, username=None, type="supergroup"),
        from_user=SimpleNamespace(id=user_id),
        message_id=message_id,
        reply_to_message=reply,
        answer=AsyncMock(),
    )


def _video():
    return SimpleNamespace(file_id="video-file-id", file_unique_id="video-unique-id")


def _video_document(mime_type="video/mp4", file_name="lecture.mp4"):
    return SimpleNamespace(
        file_id="document-file-id",
        file_unique_id="document-unique-id",
        mime_type=mime_type,
        file_name=file_name,
    )


async def _seed_item(session, *, message_id=101, media_type="video"):
    batch = UploadBatch(
        source_chat_id=INVENTORY_CHAT_ID,
        batch_date=datetime.now(timezone.utc).date(),
        status="open",
    )
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    item = UploadItem(
        batch_id=batch.id,
        source_chat_id=INVENTORY_CHAT_ID,
        source_message_id=message_id,
        media_type=media_type,
        file_id=f"file-{message_id}",
        file_unique_id=f"unique-{message_id}",
        status="received",
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def test_videoid_returns_id_for_registered_video(monkeypatch, session):
    _settings(monkeypatch)
    item = await _seed_item(session, message_id=101)
    before = (item.status, item.batch_id, item.file_id, item.raw_caption)
    message = _command(reply=_reply(message_id=101, video=_video()))

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == f"Video ID: {item.id}"
    assert message.answer.await_args.kwargs["parse_mode"] is None
    reply_parameters = message.answer.await_args.kwargs["reply_parameters"]
    assert reply_parameters.message_id == message.message_id

    await session.refresh(item)
    assert (item.status, item.batch_id, item.file_id, item.raw_caption) == before


async def test_videoid_returns_id_for_registered_video_document(monkeypatch, session):
    _settings(monkeypatch)
    item = await _seed_item(session, message_id=102, media_type="document")
    message = _command(
        reply=_reply(message_id=102, document=_video_document())
    )

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == f"Video ID: {item.id}"


async def test_videoid_accepts_video_document_without_mime_type(monkeypatch, session):
    _settings(monkeypatch)
    item = await _seed_item(session, message_id=103, media_type="document")
    document = _video_document(mime_type=None, file_name="lecture.mkv")
    message = _command(reply=_reply(message_id=103, document=document))

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == f"Video ID: {item.id}"


async def test_videoid_accepts_video_extension_with_generic_mime_type(
    monkeypatch, session
):
    _settings(monkeypatch)
    item = await _seed_item(session, message_id=104, media_type="document")
    document = _video_document(
        mime_type="application/octet-stream", file_name="lecture.mp4"
    )
    message = _command(reply=_reply(message_id=104, document=document))

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == f"Video ID: {item.id}"


async def test_videoid_is_admin_only(monkeypatch, session):
    _settings(monkeypatch)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(ingest_handler, "get_item_by_source", lookup)
    message = _command(user_id=999, reply=_reply(video=_video()))

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_not_awaited()
    lookup.assert_not_awaited()


async def test_videoid_outside_inventory_group(monkeypatch, session):
    _settings(monkeypatch)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(ingest_handler, "get_item_by_source", lookup)
    message = _command(chat_id=-9999, reply=_reply(video=_video()))

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "inventory group" in text
    assert "Video ID" not in text
    lookup.assert_not_awaited()


async def test_videoid_requires_a_reply(monkeypatch, session):
    _settings(monkeypatch)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(ingest_handler, "get_item_by_source", lookup)
    message = _command(reply=None)

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "Reply to a video" in message.answer.await_args.args[0]
    lookup.assert_not_awaited()


async def test_videoid_rejects_non_video_reply(monkeypatch, session):
    _settings(monkeypatch)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(ingest_handler, "get_item_by_source", lookup)
    message = _command(reply=_reply())

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "not a video" in message.answer.await_args.args[0]
    lookup.assert_not_awaited()


async def test_videoid_rejects_document_that_is_not_video(monkeypatch, session):
    _settings(monkeypatch)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(ingest_handler, "get_item_by_source", lookup)
    document = SimpleNamespace(
        file_id="pdf-file-id",
        file_unique_id="pdf-unique-id",
        mime_type="application/pdf",
        file_name="notes.pdf",
    )
    message = _command(reply=_reply(document=document))

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "not a video" in message.answer.await_args.args[0]
    lookup.assert_not_awaited()


async def test_videoid_reports_unregistered_video_without_creating_item(
    monkeypatch, session
):
    _settings(monkeypatch)
    message = _command(reply=_reply(message_id=777, video=_video()))

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "No upload-item ID found" in text
    assert "Video ID:" not in text
    count = await session.scalar(select(func.count(UploadItem.id)))
    assert count == 0


async def test_videoid_handles_database_errors_safely(monkeypatch, session):
    _settings(monkeypatch)
    lookup = AsyncMock(side_effect=RuntimeError("connection lost"))
    monkeypatch.setattr(ingest_handler, "get_item_by_source", lookup)
    message = _command(reply=_reply(video=_video()))

    await ingest_handler.videoid_cmd(message, session)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "Could not look up" in text
    assert "Video ID:" not in text
    assert "connection lost" not in text
    count = await session.scalar(select(func.count(UploadItem.id)))
    assert count == 0
