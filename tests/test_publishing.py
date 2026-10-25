from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from bot.db.ingestion_repo import approve_batch
from bot.db.models import UploadItem
from bot.handlers import review
from bot.services.ingestion import ingest_media
from bot.services.publishing import (
    publish_batch,
    publish_item,
    reconcile_publish_state,
    unlock_publish_state,
)


async def _configure_course(session, course, *, destination="-100123", topic=12):
    course.destination_chat_id = destination
    course.topic_id = topic
    await session.commit()


async def _ingest(
    session,
    *,
    message_id,
    caption=None,
    file_name=None,
    file_id="telegram-file-id",
    file_unique_id=None,
    media_type="video",
):
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type=media_type,
        caption=caption,
        file_name=file_name,
        file_id=file_id,
        file_unique_id=file_unique_id or f"unique-{message_id}",
    )
    return item


async def _approve(session, item):
    ok, text = await approve_batch(session, item.batch_id, 123)
    assert ok is True, text


async def test_video_publishes_to_configured_forum_topic(session, seeded_course):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=77)
    item = await _ingest(
        session,
        message_id=101,
        caption="Anatomy A Lecture 1",
        file_id="video-file-id",
    )
    await _approve(session, item)

    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=501)

    result = await publish_item(session, bot, item.id)

    assert result.status == "published"
    assert result.message_id == 501
    bot.send_video.assert_awaited_once()
    kwargs = bot.send_video.await_args.kwargs
    assert kwargs["chat_id"] == -100123
    assert kwargs["video"] == "video-file-id"
    assert kwargs["message_thread_id"] == 77
    assert kwargs["caption"] == "Anatomy A Lecture 1"
    assert kwargs["parse_mode"] is None
    assert kwargs["protect_content"] is True
    bot.send_document.assert_not_awaited()

    await session.refresh(item)
    assert item.publication_status == "published"
    assert item.published_message_id == 501
    assert item.error is None


async def test_document_publishes_to_configured_forum_topic(session, seeded_course):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100456", topic=88)
    item = await _ingest(
        session,
        message_id=102,
        file_name="Anatomy A Lecture 2.pdf",
        file_id="document-file-id",
        media_type="document",
    )
    await _approve(session, item)

    bot = AsyncMock()
    bot.send_document.return_value = SimpleNamespace(message_id=502)

    result = await publish_item(session, bot, item.id)

    assert result.status == "published"
    assert result.message_id == 502
    bot.send_document.assert_awaited_once()
    kwargs = bot.send_document.await_args.kwargs
    assert kwargs["chat_id"] == -100456
    assert kwargs["document"] == "document-file-id"
    assert kwargs["message_thread_id"] == 88
    assert kwargs["caption"] == "Anatomy A Lecture 2"
    assert kwargs["parse_mode"] is None
    assert kwargs["protect_content"] is True
    bot.send_video.assert_not_awaited()

    await session.refresh(item)
    assert item.publication_status == "published"
    assert item.published_message_id == 502


async def test_missing_destination_fails_without_sending(
    session, seeded_course, monkeypatch
):
    _, course = seeded_course
    await _configure_course(session, course, destination=None, topic=12)
    monkeypatch.setattr(
        "bot.services.publishing.get_settings",
        lambda: SimpleNamespace(destination_group_id=""),
    )
    item = await _ingest(session, message_id=103, caption="Anatomy A Lecture 3")
    await _approve(session, item)

    bot = AsyncMock()
    result = await publish_item(session, bot, item.id)

    assert result.status == "failed"
    assert "destination" in result.reason.lower()
    bot.send_video.assert_not_awaited()
    await session.refresh(item)
    assert item.publication_status == "failed"
    assert "destination" in item.error.lower()


async def test_missing_topic_fails_without_sending(session, seeded_course):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=None)
    item = await _ingest(session, message_id=104, caption="Anatomy A Lecture 4")
    await _approve(session, item)

    bot = AsyncMock()
    result = await publish_item(session, bot, item.id)

    assert result.status == "failed"
    assert "topic" in result.reason.lower()
    bot.send_video.assert_not_awaited()


async def test_destination_falls_back_to_environment(
    session, seeded_course, monkeypatch
):
    _, course = seeded_course
    await _configure_course(session, course, destination=None, topic=13)
    monkeypatch.setattr(
        "bot.services.publishing.get_settings",
        lambda: SimpleNamespace(destination_group_id="-100777"),
    )
    item = await _ingest(
        session,
        message_id=105,
        caption="Anatomy A Lecture 5",
        file_id="fallback-file-id",
    )
    await _approve(session, item)

    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=505)

    result = await publish_item(session, bot, item.id)

    assert result.status == "published"
    assert bot.send_video.await_args.kwargs["chat_id"] == -100777


async def test_publish_command_reports_published_count(session, seeded_course):
    _, course = seeded_course
    await _configure_course(session, course)
    item = await _ingest(
        session,
        message_id=112,
        caption="Anatomy A Lecture 12",
        file_id="command-file-id",
    )
    await _approve(session, item)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/publish {item.batch_id}",
        answer=AsyncMock(),
    )
    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=512)

    await review.publish_cmd(message, session, bot)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "1 published" in text
    assert "0 failed" in text


async def test_non_admin_publish_is_denied(session):
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        text="/publish 1",
        answer=AsyncMock(),
    )
    bot = AsyncMock()

    await review.publish_cmd(message, session, bot)

    message.answer.assert_not_awaited()
    bot.send_video.assert_not_awaited()
    bot.send_document.assert_not_awaited()


async def test_unapproved_batch_is_rejected(session, seeded_course):
    _, course = seeded_course
    await _configure_course(session, course)
    item = await _ingest(session, message_id=106, caption="Anatomy A Lecture 6")

    bot = AsyncMock()
    result = await publish_batch(session, bot, item.batch_id)

    assert result.error is not None
    assert "not approved" in result.error.lower()
    assert result.results == []
    bot.send_video.assert_not_awaited()
    await session.refresh(item)
    assert item.publication_status is None


async def test_missing_file_id_is_handled_safely(session, seeded_course):
    _, course = seeded_course
    await _configure_course(session, course)
    item = await _ingest(
        session,
        message_id=107,
        caption="Anatomy A Lecture 7",
        file_id=None,
    )
    await _approve(session, item)

    bot = AsyncMock()
    result = await publish_item(session, bot, item.id)

    assert result.status == "failed"
    assert "file_id" in result.reason
    bot.send_video.assert_not_awaited()
    await session.refresh(item)
    assert item.publication_status == "failed"
    assert "file_id" in item.error


async def test_already_published_item_is_not_sent_again(session, seeded_course):
    _, course = seeded_course
    await _configure_course(session, course)
    item = await _ingest(
        session,
        message_id=108,
        caption="Anatomy A Lecture 8",
        file_id="once-only-file-id",
    )
    await _approve(session, item)

    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=508)

    first = await publish_item(session, bot, item.id)
    second = await publish_item(session, bot, item.id)

    assert first.status == "published"
    assert second.status == "already_published"
    assert second.message_id == 508
    bot.send_video.assert_awaited_once()

    await session.refresh(item)
    assert item.publication_status == "published"
    assert item.published_message_id == 508


async def test_partial_failure_does_not_block_other_items_and_can_retry(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=21)
    first_item = await _ingest(
        session,
        message_id=109,
        caption="Anatomy A Lecture 9",
        file_id="partial-file-1",
    )
    second_item = await _ingest(
        session,
        message_id=110,
        caption="Anatomy A Lecture 10",
        file_id="partial-file-2",
    )
    await _approve(session, first_item)
    await session.refresh(second_item)
    assert second_item.batch_id == first_item.batch_id

    bot = AsyncMock()
    bot.send_video.side_effect = [
        SimpleNamespace(message_id=601),
        TelegramBadRequest(None, "temporary telegram failure"),
    ]

    first_result = await publish_batch(session, bot, first_item.batch_id)

    assert first_result.published == 1
    assert len(first_result.failures) == 1
    assert first_result.failures[0].item_id == second_item.id
    assert "temporary telegram failure" in first_result.failures[0].reason
    assert bot.send_video.await_count == 2

    await session.refresh(second_item)
    assert second_item.publication_status == "failed"

    bot.send_video.side_effect = [SimpleNamespace(message_id=602)]

    second_result = await publish_batch(session, bot, first_item.batch_id)

    assert second_result.published == 1
    assert second_result.already_published == 1
    assert second_result.failures == []
    assert bot.send_video.await_count == 3

    await session.refresh(first_item)
    await session.refresh(second_item)
    assert first_item.published_message_id == 601
    assert second_item.publication_status == "published"
    assert second_item.published_message_id == 602
    assert second_item.error is None


async def test_uncertain_publish_is_not_retried_until_unlocked(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course)
    item = await _ingest(
        session,
        message_id=113,
        caption="Anatomy A Lecture 13",
        file_id="uncertain-file-id",
    )
    await _approve(session, item)

    item.publication_status = "publishing"
    await session.commit()

    bot = AsyncMock()
    result = await publish_item(session, bot, item.id)

    assert result.status == "failed"
    assert "unlockpublish" in result.reason
    bot.send_video.assert_not_awaited()
    await session.refresh(item)
    assert item.publication_status == "publishing"

    unlocked, error = await unlock_publish_state(session, item.id)
    assert error is None
    assert unlocked is not None
    assert unlocked.publication_status is None

    bot.send_video.return_value = SimpleNamespace(message_id=513)
    retried = await publish_item(session, bot, item.id)

    assert retried.status == "published"
    assert retried.message_id == 513
    bot.send_video.assert_awaited_once()


async def test_ambiguous_network_failure_is_not_retried_automatically(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course)
    item = await _ingest(
        session,
        message_id=114,
        caption="Anatomy A Lecture 14",
        file_id="network-file-id",
    )
    await _approve(session, item)

    bot = AsyncMock()
    bot.send_video.side_effect = TelegramNetworkError(None, "timed out")

    first = await publish_item(session, bot, item.id)

    assert first.status == "failed"
    assert "uncertain" in first.reason.lower()
    assert "unlockpublish" in first.reason.lower()
    await session.refresh(item)
    assert item.publication_status == "publishing"
    assert item.error is not None

    retry = await publish_item(session, bot, item.id)

    assert retry.status == "failed"
    assert "unlockpublish" in retry.reason.lower()
    bot.send_video.assert_awaited_once()


async def test_missing_message_id_is_treated_as_uncertain(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course)
    item = await _ingest(
        session,
        message_id=115,
        caption="Anatomy A Lecture 15",
        file_id="missing-id-file-id",
    )
    await _approve(session, item)

    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace()

    result = await publish_item(session, bot, item.id)

    assert result.status == "failed"
    assert "uncertain" in result.reason.lower()
    await session.refresh(item)
    assert item.publication_status == "publishing"
    assert item.published_message_id is None


async def test_ingestion_stores_telegram_file_id(session, seeded_course):
    seeded_course
    item = await _ingest(
        session,
        message_id=111,
        caption="Anatomy A Lecture 11",
        file_id="stored-file-id",
    )

    assert item.file_id == "stored-file-id"
    assert item.file_unique_id == "unique-111"


async def test_flood_control_is_failed_and_preserves_retry_delay(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=31)
    item = await _ingest(
        session,
        message_id=116,
        caption="Anatomy A Lecture 16",
        file_id="flood-control-file",
    )
    await _approve(session, item)

    bot = AsyncMock()
    bot.send_video.side_effect = TelegramRetryAfter(
        None, "Too Many Requests: retry after 30", 30
    )

    result = await publish_item(session, bot, item.id)

    assert result.status == "failed"
    assert "retry after 30" in result.reason.lower()
    assert "no message was sent" in result.reason.lower()
    bot.send_video.assert_awaited_once()

    await session.refresh(item)
    assert item.publication_status == "failed"
    assert item.published_message_id is None
    assert "retry after 30" in item.error.lower()


async def test_server_error_remains_uncertain(session, seeded_course):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=32)
    item = await _ingest(
        session,
        message_id=117,
        caption="Anatomy A Lecture 17",
        file_id="server-error-file",
    )
    await _approve(session, item)

    bot = AsyncMock()
    bot.send_video.side_effect = TelegramServerError(None, "502 Bad Gateway")

    result = await publish_item(session, bot, item.id)

    assert result.status == "failed"
    assert "uncertain" in result.reason.lower()
    await session.refresh(item)
    assert item.publication_status == "publishing"
    assert item.published_message_id is None


async def test_reconcile_publish_records_existing_message_without_resend(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=41)
    item = await _ingest(
        session,
        message_id=118,
        caption="Anatomy A Lecture 18",
        file_id="reconcile-file",
    )
    await _approve(session, item)
    item.publication_status = "publishing"
    item.error = "network timeout"
    await session.commit()

    reconciled, error = await reconcile_publish_state(
        session,
        item.id,
        message_id=701,
        destination_chat_id="-100123",
        topic_id=41,
    )

    assert error is None
    assert reconciled is not None
    await session.refresh(item)
    assert item.publication_status == "published"
    assert item.published_message_id == 701
    assert item.error is None


async def test_reconcile_publish_keeps_state_if_projection_fails(
    session, seeded_course, monkeypatch
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=47)
    item = await _ingest(
        session,
        message_id=124,
        caption="Anatomy A Lecture 24",
        file_id="reconcile-projection-file",
    )
    await _approve(session, item)
    item.publication_status = "publishing"
    await session.commit()

    async def fail_projection(*args, **kwargs):
        raise RuntimeError("simulated catalogue failure")

    monkeypatch.setattr(
        "bot.services.publishing.project_upload_to_lecture",
        fail_projection,
    )

    reconciled, error = await reconcile_publish_state(
        session,
        item.id,
        message_id=707,
        destination_chat_id="-100123",
        topic_id=47,
    )

    assert error is None
    assert reconciled is not None
    await session.refresh(item)
    assert item.publication_status == "published"
    assert item.published_message_id == 707


async def test_reconcile_publish_rejects_mismatched_destination_and_topic(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=42)
    item = await _ingest(
        session,
        message_id=119,
        caption="Anatomy A Lecture 19",
        file_id="reconcile-mismatch-file",
    )
    await _approve(session, item)
    item.publication_status = "publishing"
    await session.commit()

    _, destination_error = await reconcile_publish_state(
        session,
        item.id,
        message_id=702,
        destination_chat_id="-100999",
        topic_id=42,
    )
    assert "destination" in destination_error.lower()

    _, topic_error = await reconcile_publish_state(
        session,
        item.id,
        message_id=702,
        destination_chat_id="-100123",
        topic_id=99,
    )
    assert "topic" in topic_error.lower()

    await session.refresh(item)
    assert item.publication_status == "publishing"
    assert item.published_message_id is None


async def test_reconcile_publish_rejects_wrong_state_and_bad_ids(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=43)
    item = await _ingest(
        session,
        message_id=120,
        caption="Anatomy A Lecture 20",
        file_id="reconcile-state-file",
    )
    await _approve(session, item)

    _, state_error = await reconcile_publish_state(
        session,
        item.id,
        message_id=703,
        destination_chat_id="-100123",
        topic_id=43,
    )
    assert "uncertain" in state_error.lower()

    item.publication_status = "publishing"
    await session.commit()
    _, message_error = await reconcile_publish_state(
        session,
        item.id,
        message_id=0,
        destination_chat_id="-100123",
        topic_id=43,
    )
    assert "message id" in message_error.lower()

    _, topic_error = await reconcile_publish_state(
        session,
        item.id,
        message_id=703,
        destination_chat_id="-100123",
        topic_id=0,
    )
    assert "topic id" in topic_error.lower()

    await session.refresh(item)
    assert item.publication_status == "publishing"
    assert item.published_message_id is None


async def test_reconcile_publish_requires_an_approved_batch(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=44)
    item = await _ingest(
        session,
        message_id=121,
        caption="Anatomy A Lecture 21",
        file_id="reconcile-unapproved-file",
    )
    item.publication_status = "publishing"
    await session.commit()

    _, error = await reconcile_publish_state(
        session,
        item.id,
        message_id=704,
        destination_chat_id="-100123",
        topic_id=44,
    )

    assert "not approved" in error.lower()
    await session.refresh(item)
    assert item.publication_status == "publishing"


async def test_reconcile_publish_rolls_back_on_commit_failure(
    db, session, seeded_course, monkeypatch
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=45)
    item = await _ingest(
        session,
        message_id=122,
        caption="Anatomy A Lecture 22",
        file_id="reconcile-rollback-file",
    )
    await _approve(session, item)
    item.publication_status = "publishing"
    await session.commit()
    item_id = item.id

    async def fail_commit():
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(session, "commit", fail_commit)

    reconciled, error = await reconcile_publish_state(
        session,
        item_id,
        message_id=705,
        destination_chat_id="-100123",
        topic_id=45,
    )

    assert reconciled is None
    assert "no changes were applied" in error.lower()

    _, factory = db
    async with factory() as check:
        stored = await check.get(UploadItem, item_id)
        assert stored.publication_status == "publishing"
        assert stored.published_message_id is None


async def test_reconcile_publish_command_records_existing_message(
    session, seeded_course
):
    _, course = seeded_course
    await _configure_course(session, course, destination="-100123", topic=46)
    item = await _ingest(
        session,
        message_id=123,
        caption="Anatomy A Lecture 23",
        file_id="reconcile-command-file",
    )
    await _approve(session, item)
    item.publication_status = "publishing"
    await session.commit()

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/reconcilepublish {item.id}|706|-100123|46",
        answer=AsyncMock(),
    )

    await review.reconcilepublish_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "reconciled" in message.answer.await_args.args[0].lower()
    await session.refresh(item)
    assert item.publication_status == "published"
    assert item.published_message_id == 706
