from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from bot.db.models import (
    Lecture,
    StudentMediaMessage,
    Subject,
    UploadBatch,
    UploadItem,
    User,
)
from bot.handlers.lectures import watch_lecture
from bot.services.access import APPROVED
from bot.services.media_delivery import (
    STATUS_DELETED,
    STATUS_MISSED,
    process_due_deletions,
    record_delivered_media,
    retention_hours,
)


def _callback(data, user_id=9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


async def _student(session, user_id=9001):
    user = User(
        id=user_id,
        username="student",
        full_name="Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_retention_hours_is_clamped_to_telegram_safe_range():
    assert retention_hours(-5) == 1
    assert retention_hours(0) == 1
    assert retention_hours(24) == 24
    assert retention_hours(100) == 48


async def test_watch_records_student_media_for_automatic_cleanup(session):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="A", order=1)
    batch = UploadBatch(
        source_chat_id=-1001,
        batch_date=date.today(),
        status="approved",
    )
    session.add_all([subject, batch])
    await session.commit()
    await session.refresh(subject)
    await session.refresh(batch)

    upload = UploadItem(
        batch_id=batch.id,
        source_chat_id=-1001,
        source_message_id=1,
        media_type="video",
        file_id="tracked-video-file",
        status="published",
        publication_status="published",
    )
    session.add(upload)
    await session.commit()
    await session.refresh(upload)

    lecture = Lecture(
        subject_id=subject.id,
        source_upload_item_id=upload.id,
        title="Tracked Lecture",
        order=1,
    )
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)

    user = await _student(session)
    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=555)
    callback = _callback(f"watch:{lecture.id}", user.id)
    before = datetime.utcnow()

    await watch_lecture(callback, session, user, bot)

    record = await session.scalar(select(StudentMediaMessage))
    assert record is not None
    assert record.user_id == user.id
    assert record.lecture_id == lecture.id
    assert record.chat_id == user.id
    assert record.message_id == 555
    assert record.status == "pending"
    assert record.delete_at >= before + timedelta(hours=23, minutes=59)
    assert record.delete_at <= datetime.utcnow() + timedelta(hours=24, minutes=1)


async def test_record_delivered_media_sets_a_bounded_deadline(session):
    sent_at = datetime.utcnow() - timedelta(minutes=5)

    record = await record_delivered_media(
        session,
        user_id=9001,
        lecture_id=None,
        chat_id=9001,
        message_id=42,
        sent_at=sent_at,
        retention=48,
    )

    assert record is not None
    assert record.delete_at == sent_at + timedelta(hours=48) - timedelta(
        minutes=5
    )
    assert record.sent_at == sent_at


async def test_cleanup_deletes_only_due_student_media(db):
    _, factory = db
    now = datetime.utcnow()
    async with factory() as session:
        record = StudentMediaMessage(
            user_id=9001,
            lecture_id=None,
            chat_id=9001,
            message_id=42,
            sent_at=now - timedelta(hours=25),
            delete_at=now - timedelta(minutes=1),
            status="pending",
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        record_id = record.id

    bot = AsyncMock()

    deleted = await process_due_deletions(
        bot, session_factory=factory, now=now
    )

    assert deleted == 1
    bot.delete_message.assert_awaited_once_with(
        chat_id=9001, message_id=42
    )
    async with factory() as check:
        record = await check.get(StudentMediaMessage, record_id)
        assert record.status == STATUS_DELETED
        assert record.deleted_at == now


async def test_cleanup_handles_multiple_due_messages(db):
    _, factory = db
    now = datetime.utcnow()
    async with factory() as session:
        for message_id in (51, 52):
            session.add(
                StudentMediaMessage(
                    user_id=9001,
                    lecture_id=None,
                    chat_id=9001,
                    message_id=message_id,
                    sent_at=now - timedelta(hours=25),
                    delete_at=now - timedelta(minutes=1),
                    status="pending",
                )
            )
        await session.commit()

    bot = AsyncMock()

    assert await process_due_deletions(
        bot, session_factory=factory, now=now
    ) == 2
    deleted_ids = sorted(
        call.kwargs["message_id"]
        for call in bot.delete_message.await_args_list
    )
    assert deleted_ids == [51, 52]


async def test_cleanup_skips_messages_that_are_not_due(db):
    _, factory = db
    now = datetime.utcnow()
    async with factory() as session:
        session.add(
            StudentMediaMessage(
                user_id=9001,
                lecture_id=None,
                chat_id=9001,
                message_id=43,
                sent_at=now - timedelta(hours=1),
                delete_at=now + timedelta(hours=1),
                status="pending",
            )
        )
        await session.commit()

    bot = AsyncMock()

    assert await process_due_deletions(
        bot, session_factory=factory, now=now
    ) == 0
    bot.delete_message.assert_not_awaited()


async def test_cleanup_respects_telegram_48_hour_limit(db):
    _, factory = db
    now = datetime.utcnow()
    async with factory() as session:
        record = StudentMediaMessage(
            user_id=9001,
            lecture_id=None,
            chat_id=9001,
            message_id=44,
            sent_at=now - timedelta(hours=49),
            delete_at=now - timedelta(hours=1),
            status="pending",
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        record_id = record.id

    bot = AsyncMock()

    assert await process_due_deletions(
        bot, session_factory=factory, now=now
    ) == 0
    bot.delete_message.assert_not_awaited()
    async with factory() as check:
        record = await check.get(StudentMediaMessage, record_id)
        assert record.status == STATUS_MISSED
        assert record.deleted_at is None
