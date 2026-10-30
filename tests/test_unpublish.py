from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select

from bot.db.models import Lecture, RollbackItem
from bot.db.repo import list_lectures
from bot.handlers import review
from bot.services.catalog import project_upload_to_lecture
from bot.services.ingestion import ingest_media
from bot.services.publishing import publish_item
from bot.services.rollback import (
    CHANNEL_ALREADY_DELETED,
    CHANNEL_DELETED,
    COMPLETED,
    PARTIAL,
    cancel_operation,
    create_preview,
    execute_operation,
    get_operation_items,
)


async def _published_lecture(
    session,
    seeded_course,
    *,
    message_id: int,
    published_message_id: int,
    title: str,
    withdrawn: bool = False,
):
    subject, course = seeded_course
    course.destination_chat_id = "-100123"
    course.topic_id = 12
    await session.commit()
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type="video",
        caption=f"Anatomy A Lecture {message_id}",
        file_name=None,
        file_id=f"file-{message_id}",
        file_unique_id=f"unique-unpublish-{message_id}",
    )
    item.course_id = course.id
    item.title = title
    item.lecture_number = message_id
    item.publication_status = "withdrawn" if withdrawn else "published"
    item.published_message_id = published_message_id
    item.published_chat_id = "-100123"
    item.published_topic_id = 12
    lecture = Lecture(
        subject_id=subject.id,
        source_upload_item_id=item.id,
        title=title,
        order=message_id,
        is_withdrawn=withdrawn,
        withdrawal_status="withdrawn" if withdrawn else None,
    )
    session.add(lecture)
    await session.commit()
    await session.refresh(item)
    await session.refresh(lecture)
    return item, lecture


async def test_preview_snapshots_target_without_changing_anything(session, seeded_course):
    item, lecture = await _published_lecture(
        session,
        seeded_course,
        message_id=1,
        published_message_id=501,
        title="Thorax",
    )

    preview, error = await create_preview(session, 123, lecture_id=lecture.id)

    assert error is None
    assert preview.operation.status == "pending"
    assert preview.operation.total_count == 1
    snapshot = preview.items[0]
    assert snapshot.lecture_id == lecture.id
    assert snapshot.upload_item_id == item.id
    assert snapshot.target_chat_id == "-100123"
    assert snapshot.published_message_id == 501
    await session.refresh(lecture)
    await session.refresh(item)
    assert lecture.is_withdrawn is False
    assert item.publication_status == "published"


async def test_confirm_deletes_channel_message_and_withdraws_catalogue(
    session, seeded_course
):
    item, lecture = await _published_lecture(
        session,
        seeded_course,
        message_id=2,
        published_message_id=502,
        title="Abdomen",
    )
    preview, _ = await create_preview(session, 123, lecture_id=lecture.id)
    bot = AsyncMock()
    bot.delete_message.return_value = True

    operation, error = await execute_operation(
        session, bot, preview.operation.id, 123
    )

    assert error is None
    assert operation.status == COMPLETED
    bot.delete_message.assert_awaited_once_with(
        chat_id="-100123", message_id=502
    )
    await session.refresh(lecture)
    await session.refresh(item)
    assert lecture.is_withdrawn is True
    assert lecture.withdrawal_status == "withdrawn"
    assert lecture.withdrawn_at is not None
    assert item.publication_status == "withdrawn"
    snapshot = (await get_operation_items(session, operation.id))[0]
    assert snapshot.channel_status == CHANNEL_DELETED
    assert snapshot.catalogue_status == "withdrawn"


async def test_already_deleted_message_is_treated_as_success(session, seeded_course):
    _, lecture = await _published_lecture(
        session,
        seeded_course,
        message_id=3,
        published_message_id=503,
        title="Pelvis",
    )
    preview, _ = await create_preview(session, 123, lecture_id=lecture.id)
    bot = AsyncMock()
    bot.delete_message.side_effect = TelegramBadRequest(
        None, "Bad Request: message to delete not found"
    )

    operation, error = await execute_operation(
        session, bot, preview.operation.id, 123
    )

    assert error is None
    assert operation.status == COMPLETED
    snapshot = (await get_operation_items(session, operation.id))[0]
    assert snapshot.channel_status == CHANNEL_ALREADY_DELETED
    assert snapshot.catalogue_status == "withdrawn"


async def test_unconfirmed_delete_result_is_a_channel_failure(session, seeded_course):
    _, lecture = await _published_lecture(
        session,
        seeded_course,
        message_id=11,
        published_message_id=511,
        title="Embryology",
    )
    preview, _ = await create_preview(session, 123, lecture_id=lecture.id)
    bot = AsyncMock()
    bot.delete_message.return_value = False

    operation, error = await execute_operation(
        session, bot, preview.operation.id, 123
    )

    assert error is None
    assert operation.status == PARTIAL
    snapshot = (await get_operation_items(session, operation.id))[0]
    assert snapshot.channel_status == "failed"
    assert snapshot.catalogue_status == "withdrawn"


async def test_cancel_prevents_execution(session, seeded_course):
    _, lecture = await _published_lecture(
        session,
        seeded_course,
        message_id=12,
        published_message_id=512,
        title="Histology",
    )
    preview, _ = await create_preview(session, 123, lecture_id=lecture.id)

    assert await cancel_operation(session, preview.operation.id, 123) is True
    bot = AsyncMock()
    operation, error = await execute_operation(
        session, bot, preview.operation.id, 123
    )

    assert operation.status == "cancelled"
    assert "no longer executable" in error
    bot.delete_message.assert_not_awaited()


async def test_channel_failure_is_partial_and_retry_skips_successful_catalogue(
    session, seeded_course
):
    _, lecture = await _published_lecture(
        session,
        seeded_course,
        message_id=4,
        published_message_id=504,
        title="Head and Neck",
    )
    preview, _ = await create_preview(session, 123, lecture_id=lecture.id)
    bot = AsyncMock()
    bot.delete_message.side_effect = TelegramForbiddenError(None, "Forbidden")

    first, error = await execute_operation(
        session, bot, preview.operation.id, 123
    )

    assert error is None
    assert first.status == PARTIAL
    first_item = (await get_operation_items(session, first.id))[0]
    assert first_item.channel_status == "failed"
    assert first_item.catalogue_status == "withdrawn"
    await session.refresh(lecture)
    assert lecture.is_withdrawn is True
    assert lecture.withdrawal_status == PARTIAL

    retry_preview, retry_error = await create_preview(
        session, 123, lecture_id=lecture.id
    )
    assert retry_error is None
    bot.delete_message.side_effect = None
    bot.delete_message.return_value = True
    second, retry_error = await execute_operation(
        session, bot, retry_preview.operation.id, 123
    )

    assert retry_error is None
    assert second.status == COMPLETED
    assert bot.delete_message.await_count == 2
    second_item = (await get_operation_items(session, second.id))[0]
    assert second_item.channel_status == CHANNEL_DELETED
    assert second_item.catalogue_status == "already_withdrawn"
    await session.refresh(lecture)
    assert lecture.withdrawal_status == "withdrawn"


async def test_unpublish_commands_preview_and_callback(session, seeded_course):
    _, lecture = await _published_lecture(
        session,
        seeded_course,
        message_id=5,
        published_message_id=505,
        title="Upper Limb",
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/unpublish {lecture.id}",
        answer=AsyncMock(),
    )

    await review.unpublish_cmd(message, session)

    text = message.answer.await_args.args[0]
    assert "UNPUBLISH PREVIEW" in text
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    operation_id = keyboard.inline_keyboard[0][0].callback_data.rsplit(":", 1)[1]
    bot = AsyncMock()
    bot.delete_message.return_value = True
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        data=f"unpublish:confirm:{operation_id}",
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )

    await review.unpublish_callback(callback, session, bot)

    bot.delete_message.assert_awaited_once_with(
        chat_id="-100123", message_id=505
    )
    edited = callback.message.edit_text.await_args.args[0]
    assert "completed" in edited


async def test_unpublishbatch_skips_already_withdrawn(session, seeded_course):
    active, _ = await _published_lecture(
        session,
        seeded_course,
        message_id=6,
        published_message_id=506,
        title="Lower Limb",
    )
    await _published_lecture(
        session,
        seeded_course,
        message_id=7,
        published_message_id=507,
        title="Back",
        withdrawn=True,
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/unpublishbatch {active.batch_id}",
        answer=AsyncMock(),
    )

    await review.unpublishbatch_cmd(message, session)

    text = message.answer.await_args.args[0]
    assert "Lectures: 1" in text
    assert "Already withdrawn and skipped: 1" in text


async def test_withdrawn_lecture_is_hidden_and_cannot_be_republished(
    session, seeded_course
):
    subject, _ = seeded_course
    active_item, active = await _published_lecture(
        session,
        seeded_course,
        message_id=9,
        published_message_id=509,
        title="Active lecture",
    )
    withdrawn_item, withdrawn = await _published_lecture(
        session,
        seeded_course,
        message_id=10,
        published_message_id=510,
        title="Withdrawn lecture",
        withdrawn=True,
    )
    await session.refresh(active)
    await session.refresh(withdrawn)

    lectures = await list_lectures(session, subject.id)
    assert [lecture.id for lecture in lectures] == [active.id]

    bot = AsyncMock()
    result = await publish_item(session, bot, withdrawn_item.id)
    assert result.status == "failed"
    assert "withdrawn" in result.reason
    bot.send_video.assert_not_awaited()

    projected, created = await project_upload_to_lecture(session, withdrawn_item)
    assert projected is None
    assert created is False


def test_batch_summary_separates_withdrawn_and_unpublished():
    batch = SimpleNamespace(id=7, name="Batch", status="approved")
    items = [
        SimpleNamespace(
            id=1,
            status="ready",
            publication_status="published",
            published_message_id=11,
        ),
        SimpleNamespace(
            id=2,
            status="ready",
            publication_status="withdrawn",
            published_message_id=12,
        ),
        SimpleNamespace(
            id=3,
            status="ready",
            publication_status=None,
            published_message_id=None,
        ),
    ]

    lines = review._batch_summary(batch, items)

    text = "\n".join(lines)
    assert "Published: 1" in text
    assert "Withdrawn: 1" in text
    assert "Unpublished/unapproved: 1" in text


async def test_non_admin_cannot_preview_unpublish(session, seeded_course):
    _, lecture = await _published_lecture(
        session,
        seeded_course,
        message_id=8,
        published_message_id=508,
        title="Neuroanatomy",
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        text=f"/unpublish {lecture.id}",
        answer=AsyncMock(),
    )

    await review.unpublish_cmd(message, session)

    message.answer.assert_not_awaited()
    result = await session.execute(
        select(RollbackItem).where(RollbackItem.lecture_id == lecture.id)
    )
    assert result.scalars().all() == []
