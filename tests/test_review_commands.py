from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest

from bot.db.ingestion_repo import approve_batch, skip_item
from bot.db.models import Lecture
from bot.db.repo import add_teacher, add_topic
from bot.handlers import review
from bot.services.ingestion import ingest_media, set_item_caption

PREFERRED_CAPTION = "Anatomy A | Dr A | Skull | Introduction"
STALE_CAPTION = "Anatomy A | Skull | Lecture 2 | Dr A"


async def _prepared_item(
    session,
    seeded_course,
    *,
    message_id=60,
    caption=STALE_CAPTION,
    unique_id=None,
):
    subject, _ = seeded_course
    teacher, error = await add_teacher(session, subject.id, "Dr A")
    assert error is None
    topic, error = await add_topic(session, subject.id, "Skull")
    assert error is None
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type="video",
        caption=caption,
        file_name=None,
        file_unique_id=unique_id or f"refresh-{message_id}",
    )
    return item, teacher, topic


def _admin_message(batch_id, *, apply=False):
    suffix = " apply" if apply else ""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/refreshcaptions {batch_id}{suffix}",
        answer=AsyncMock(),
    )


def _answer_text(message):
    return "\n".join(call.args[0] for call in message.answer.await_args_list)


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


async def test_setcaption_usage_is_sent_as_plain_text():
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text="/setcaption",
        answer=AsyncMock(),
    )

    await review.setcaption_cmd(message, None)

    message.answer.assert_awaited_once()
    assert "<item_id>" in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs == {"parse_mode": None}


async def test_setcaption_success_reply_uses_plain_text_for_special_titles():
    item = SimpleNamespace(
        id=77,
        status="needs_review",
        lecture_number=8,
        title="_Cardio_ *Exam* [Part 1] `code`",
        duplicate_flag=False,
        review_reason=None,
        teacher_review_reason=None,
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setcaption {item.id}|Anatomy A Lecture 8",
        answer=AsyncMock(),
    )
    set_caption = AsyncMock(return_value=(item, None))

    with patch.object(review, "set_item_caption", set_caption):
        await review.setcaption_cmd(message, object())

    set_caption.assert_awaited_once()
    message.answer.assert_awaited_once()
    assert item.title in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs == {"parse_mode": None}


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


async def test_refreshcaptions_preview_reports_source_without_changes(
    session, seeded_course
):
    item, _, _ = await _prepared_item(session, seeded_course)
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=900, caption=PREFERRED_CAPTION
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id)

    await review.refreshcaptions_cmd(message, session, bot)

    text = _answer_text(message)
    assert "Caption refresh preview" in text
    assert "No database changes made" in text
    assert STALE_CAPTION in text
    assert PREFERRED_CAPTION in text
    bot.forward_message.assert_awaited_once_with(
        chat_id=123,
        from_chat_id=-1001,
        message_id=item.source_message_id,
        disable_notification=True,
    )
    bot.delete_message.assert_awaited_once_with(
        chat_id=123, message_id=900
    )
    assert all(
        call.kwargs == {"parse_mode": None}
        for call in message.answer.await_args_list
    )

    await session.refresh(item)
    assert item.raw_caption == STALE_CAPTION
    assert item.original_caption == STALE_CAPTION
    assert item.status == "classified"


async def test_refreshcaptions_apply_updates_non_preferred_caption(
    session, seeded_course
):
    item, teacher, topic = await _prepared_item(session, seeded_course)
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=901, caption=PREFERRED_CAPTION
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == PREFERRED_CAPTION
    assert item.original_caption == STALE_CAPTION
    assert item.title == "Introduction"
    assert item.lecture_number is None
    assert item.teacher_id == teacher.id
    assert item.status == "classified"
    assert "1 updated" in _answer_text(message)


async def test_refreshcaptions_apply_preserves_valid_manual_caption_and_reclassifies(
    session, seeded_course
):
    item, _, _ = await _prepared_item(session, seeded_course)
    item, error = await set_item_caption(session, item.id, PREFERRED_CAPTION)
    assert error is None
    item.status = "needs_review"
    item.review_reason = "topic not found: Skull"
    item.topic_review_reason = "topic not found: Skull"
    await session.commit()

    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=902,
        caption="Anatomy A | Dr A | Skull | Other Title",
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == PREFERRED_CAPTION
    assert item.original_caption == STALE_CAPTION
    assert item.title == "Introduction"
    assert item.status == "classified"
    text = _answer_text(message)
    assert "1 reclassified" in text
    assert "current caption reclassified" in text


async def test_refreshcaptions_apply_keeps_already_correct_caption(
    session, seeded_course
):
    item, _, _ = await _prepared_item(
        session, seeded_course, caption=PREFERRED_CAPTION
    )
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=903, caption=STALE_CAPTION
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == PREFERRED_CAPTION
    assert item.status == "classified"
    assert "1 preserved" in _answer_text(message)


async def test_refreshcaptions_apply_handles_partial_forward_failures(
    session, seeded_course
):
    first, _, _ = await _prepared_item(
        session, seeded_course, message_id=70
    )
    second, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=71,
        media_type="video",
        caption=STALE_CAPTION,
        file_name=None,
        file_unique_id="refresh-71",
    )

    async def forward(**kwargs):
        if kwargs["message_id"] == 71:
            raise TelegramBadRequest(None, "message to forward not found")
        return SimpleNamespace(message_id=904, caption=PREFERRED_CAPTION)

    bot = AsyncMock()
    bot.forward_message.side_effect = forward
    bot.delete_message.return_value = True
    message = _admin_message(first.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(first)
    await session.refresh(second)
    assert first.raw_caption == PREFERRED_CAPTION
    assert second.raw_caption == STALE_CAPTION
    text = _answer_text(message)
    assert "1 updated" in text
    assert "1 skipped" in text
    assert "unavailable" in text
    bot.delete_message.assert_awaited_once_with(
        chat_id=123, message_id=904
    )


async def test_refreshcaptions_apply_refuses_approved_batch(
    session, seeded_course
):
    item, _, _ = await _prepared_item(session, seeded_course)
    ok, reason = await approve_batch(session, item.batch_id, 123)
    assert ok is True, reason

    bot = AsyncMock()
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    text = _answer_text(message)
    assert "approved" in text
    assert "no changes" in text
    bot.forward_message.assert_not_awaited()
    await session.refresh(item)
    assert item.raw_caption == STALE_CAPTION


async def test_refreshcaptions_apply_skips_unusable_source_caption(
    session, seeded_course
):
    item, _, _ = await _prepared_item(session, seeded_course)
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=905, caption="not a structured caption"
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == STALE_CAPTION
    text = _answer_text(message)
    assert "1 skipped" in text
    assert "preferred layout" in text


async def test_refreshcaptions_apply_reclassifies_valid_caption_without_source(
    session, seeded_course
):
    item, _, _ = await _prepared_item(
        session, seeded_course, caption=PREFERRED_CAPTION
    )
    item.status = "needs_review"
    item.review_reason = "teacher not found: Introduction"
    item.teacher_review_reason = "teacher not found: Introduction"
    await session.commit()

    bot = AsyncMock()
    bot.forward_message.side_effect = TelegramBadRequest(
        None, "message to forward not found"
    )
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == PREFERRED_CAPTION
    assert item.status == "classified"
    text = _answer_text(message)
    assert "1 reclassified" in text
    assert "source unavailable" in text
