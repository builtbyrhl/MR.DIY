from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.db.ingestion_repo import approve_batch, skip_item
from bot.db.models import Lecture
from bot.handlers import review
from bot.services.ingestion import ingest_media


async def test_skip_command_explains_approved_batch(session, seeded_course):
    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=50,
        media_type="video",
        caption="Anatomy A Lecture 50",
        file_name=None,
        file_unique_id="approved-skip",
    )
    ok, _ = await approve_batch(session, item.batch_id, 123)
    assert ok is True

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/skip {item.id}",
        answer=AsyncMock(),
    )
    await review.skip_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "approved" in message.answer.await_args.args[0].lower()


async def test_setcaption_rejects_skipped_item_without_reviving_it(
    session, seeded_course
):
    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=51,
        media_type="video",
        caption="Anatomy A Lecture 51",
        file_name=None,
        file_unique_id="skipped-setcaption",
    )
    assert await skip_item(session, item.id) is True
    original_caption = item.raw_caption

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setcaption {item.id}|Anatomy A Lecture 99",
        answer=AsyncMock(),
    )
    await review.setcaption_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "skipped" in message.answer.await_args.args[0].lower()
    await session.refresh(item)
    assert item.status == "skipped"
    assert item.raw_caption == original_caption


async def test_setlecturetitle_updates_without_reupload(
    session, seeded_course
):
    subject, _ = seeded_course
    lecture = Lecture(subject_id=subject.id, title="Anatomy", order=1)
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setlecturetitle {lecture.id}|Brainstem",
        answer=AsyncMock(),
    )
    await review.setlecturetitle_cmd(message, session)

    await session.refresh(lecture)
    assert lecture.title == "Brainstem"
    assert "Brainstem" in message.answer.await_args.args[0]


async def test_keep_restores_skipped_item_before_caption_edit(
    session, seeded_course
):
    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=52,
        media_type="video",
        caption="Anatomy A Lecture 52",
        file_name=None,
        file_unique_id="keep-setcaption",
    )
    assert await skip_item(session, item.id) is True

    keep_message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/keep {item.id}",
        answer=AsyncMock(),
    )
    await review.keep_cmd(keep_message, session)
    await session.refresh(item)
    assert item.status != "skipped"

    caption_message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setcaption {item.id}|Anatomy A Lecture 53",
        answer=AsyncMock(),
    )
    await review.setcaption_cmd(caption_message, session)

    await session.refresh(item)
    assert item.raw_caption == "Anatomy A Lecture 53"
    assert item.lecture_number == 53
    assert "updated" in caption_message.answer.await_args.args[0].lower()
