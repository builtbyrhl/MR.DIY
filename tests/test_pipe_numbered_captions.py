from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest

from bot.db.ingestion_repo import approve_batch, get_batch_items
from bot.db.models import Course, Subject
from bot.db.repo import add_teacher, add_topic, list_item_topics
from bot.handlers import review
from bot.services.ingestion import (
    ingest_media,
    parse_caption,
    preferred_caption_error,
)
from bot.student_ui import resolve_lecture_title


@pytest_asyncio.fixture
async def pipe_numbered_catalogue(session):
    anatomy = Subject(slug="anatomy", name="Anatomy", emoji="🫀", order=1)
    physiology = Subject(slug="physiology", name="Physiology", emoji="⚡", order=2)
    session.add_all([anatomy, physiology])
    await session.commit()
    await session.refresh(anatomy)
    await session.refresh(physiology)

    anatomy_course = Course(
        year="", subject_id=anatomy.id, name="Anatomy", aliases="anatomy"
    )
    physiology_course = Course(
        year="",
        subject_id=physiology.id,
        name="Physiology",
        aliases="physiology",
    )
    session.add_all([anatomy_course, physiology_course])
    await session.commit()
    await session.refresh(anatomy_course)
    await session.refresh(physiology_course)

    anatomy_teacher, error = await add_teacher(
        session, anatomy.id, "Dr Mohammad Azam Sir"
    )
    assert error is None
    physiology_teacher, error = await add_teacher(
        session, physiology.id, "Dr Physio"
    )
    assert error is None
    abdomen, error = await add_topic(session, anatomy.id, "Abdomen")
    assert error is None
    skull, error = await add_topic(session, anatomy.id, "Skull")
    assert error is None
    brain, error = await add_topic(session, physiology.id, "Brain")
    assert error is None
    return SimpleNamespace(
        anatomy=anatomy,
        physiology=physiology,
        anatomy_course=anatomy_course,
        physiology_course=physiology_course,
        anatomy_teacher=anatomy_teacher,
        physiology_teacher=physiology_teacher,
        abdomen=abdomen,
        skull=skull,
        brain=brain,
    )


async def _ingest(session, message_id, caption):
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type="video",
        caption=caption,
        file_name=None,
        file_id=f"file-{message_id}",
        file_unique_id=f"unique-{message_id}",
    )
    return item


def _refresh_message(batch_id, *, apply=False):
    suffix = " apply" if apply else ""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/refreshcaptions {batch_id}{suffix}",
        answer=AsyncMock(),
    )


def _answer_text(message):
    return "\n".join(call.args[0] for call in message.answer.await_args_list)


def test_pipe_numbered_caption_parses_final_number_field():
    for caption, number, title in (
        (
            "Anatomy | Dr A | Abdomen | Liver Kidney | L1",
            1,
            "Liver Kidney",
        ),
        (
            "Anatomy | Dr A | Abdomen | Liver Kidney | L8",
            8,
            "Liver Kidney",
        ),
        (
            "Anatomy | Dr A | Abdomen | Portal Vein | L18",
            18,
            "Portal Vein",
        ),
        (
            "Anatomy | Dr A | Abdomen | Portal Vein | l18",
            18,
            "Portal Vein",
        ),
    ):
        parsed = parse_caption(caption, None)
        assert parsed.structured is True
        assert parsed.caption_format == "pipe_numbered"
        assert parsed.subject_name == "Anatomy"
        assert parsed.teacher_name == "Dr A"
        assert parsed.topic_name == "Abdomen"
        assert parsed.lecture_number == number
        assert parsed.numbers == [number]
        assert parsed.lecture_title == title
        assert parsed.title == title
        assert parsed.number_required is True
        assert parsed.format_error is None
        assert preferred_caption_error(caption, None) is None


def test_pipe_numbered_caption_keeps_numbers_inside_the_title():
    titled = parse_caption(
        "Anatomy | Dr A | Abdomen | L2 Overview | L3", None
    )
    assert titled.caption_format == "pipe_numbered"
    assert titled.lecture_number == 3
    assert titled.numbers == [3]
    assert titled.lecture_title == "L2 Overview"
    assert titled.title == "L2 Overview"

    numeric_title = parse_caption(
        "Anatomy | Dr A | Abdomen | Liver 8 | L8", None
    )
    assert numeric_title.caption_format == "pipe_numbered"
    assert numeric_title.lecture_number == 8
    assert numeric_title.numbers == [8]
    assert numeric_title.title == "Liver 8"


def test_pipe_numbered_caption_rejects_invalid_numbers_without_losing_title():
    for value in ("L0", "L1000", "L2 extra", "LL2", "Lecture 2", "2", "L 8"):
        parsed = parse_caption(
            f"Anatomy | Dr A | Abdomen | Liver Kidney | {value}", None
        )
        assert parsed.structured is True
        assert parsed.caption_format == "pipe_numbered"
        assert parsed.lecture_number is None
        assert parsed.numbers == []
        assert parsed.title == "Liver Kidney"
        assert parsed.format_error == f"invalid lecture number: {value}"
        assert (
            preferred_caption_error(
                f"Anatomy | Dr A | Abdomen | Liver Kidney | {value}", None
            )
            == f"invalid lecture number: {value}"
        )

    missing = parse_caption(
        "Anatomy | Dr A | Abdomen | Liver Kidney |", None
    )
    assert missing.caption_format == "pipe_numbered"
    assert missing.lecture_number is None
    assert missing.title == "Liver Kidney"
    assert missing.format_error == "lecture number missing"
    assert (
        preferred_caption_error(
            "Anatomy | Dr A | Abdomen | Liver Kidney |", None
        )
        == "lecture number missing"
    )


def test_existing_pipe_captions_keep_their_previous_behavior():
    no_number = parse_caption(
        "Anatomy | Dr A | Abdomen | Liver Kidney", None
    )
    assert no_number.caption_format == "pipe_preferred"
    assert no_number.lecture_number is None
    assert no_number.numbers == []
    assert no_number.title == "Liver Kidney"
    assert preferred_caption_error(
        "Anatomy | Dr A | Abdomen | Liver Kidney", None
    ) is None

    titled_tail = parse_caption(
        "Anatomy | Dr A | Abdomen | Part One | Part Two", None
    )
    assert titled_tail.caption_format == "pipe_preferred"
    assert titled_tail.lecture_number is None
    assert titled_tail.numbers == []
    assert titled_tail.title == "Part One | Part Two"

    legacy = parse_caption("Anatomy | Abdomen | L8 | Dr A", None)
    assert legacy.caption_format == "pipe_legacy"
    assert legacy.lecture_number == 8
    assert legacy.numbers == [8]
    assert legacy.topic_name == "Abdomen"
    assert legacy.title == "Abdomen"


async def test_pipe_numbered_caption_stores_number_and_resolves_metadata(
    session, pipe_numbered_catalogue
):
    cat = pipe_numbered_catalogue
    item = await _ingest(
        session,
        1,
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8",
    )
    assert item.status == "classified"
    assert item.course_id == cat.anatomy_course.id
    assert item.teacher_id == cat.anatomy_teacher.id
    assert item.lecture_number == 8
    assert item.title == "Liver Kidney"
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.abdomen.id
    ]

    invalid = await _ingest(
        session,
        2,
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L0",
    )
    assert invalid.status == "needs_review"
    assert invalid.lecture_number is None
    assert "invalid lecture number: L0" in invalid.review_reason
    assert [topic.id for topic in await list_item_topics(session, invalid.id)] == [
        cat.abdomen.id
    ]


async def test_pipe_numbered_caption_duplicate_is_scoped_to_course_and_topic(
    session, pipe_numbered_catalogue
):
    cat = pipe_numbered_catalogue
    first = await _ingest(
        session,
        10,
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L1",
    )
    duplicate = await _ingest(
        session,
        11,
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Portal Vein | L1",
    )
    other_topic = await _ingest(
        session,
        12,
        "Anatomy | Dr Mohammad Azam Sir | Skull | Brainstem | L1",
    )
    other_course = await _ingest(
        session,
        13,
        "Physiology | Dr Physio | Brain | Brainstem | L1",
    )

    assert first.status == "classified"
    assert duplicate.status == "needs_review"
    assert duplicate.lecture_number == 1
    assert "duplicate lecture number L1" in duplicate.review_reason
    assert f"item #{first.id}" in duplicate.review_reason
    assert other_topic.status == "classified"
    assert other_course.status == "classified"


async def test_pipe_numbered_captions_sort_numerically_after_shuffled_uploads(
    session, pipe_numbered_catalogue
):
    cat = pipe_numbered_catalogue
    for message_id, number, title in (
        (20, 3, "Third"),
        (21, 1, "First"),
        (22, 2, "Second"),
    ):
        await _ingest(
            session,
            message_id,
            f"Anatomy | Dr Mohammad Azam Sir | Abdomen | {title} | L{number}",
        )

    items = await get_batch_items(session, 1)
    assert [(item.lecture_number, item.title) for item in items] == [
        (1, "First"),
        (2, "Second"),
        (3, "Third"),
    ]
    assert [topic.id for topic in await list_item_topics(session, items[0].id)] == [
        cat.abdomen.id
    ]


async def test_refresh_preview_and_apply_store_number_from_current_pipe_caption(
    session, pipe_numbered_catalogue
):
    item = await _ingest(
        session,
        30,
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8",
    )
    item.lecture_number = None
    await session.commit()

    bot = AsyncMock()
    bot.forward_message.side_effect = TelegramBadRequest(
        None, "message to forward not found"
    )

    preview = _refresh_message(item.batch_id)
    await review.refreshcaptions_cmd(preview, session, bot)
    preview_text = _answer_text(preview)
    assert "action: reclassify" in preview_text
    assert "current=L8" in preview_text
    assert "proposed=L8" in preview_text

    await session.refresh(item)
    assert item.raw_caption == (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
    )
    assert item.lecture_number is None

    apply = _refresh_message(item.batch_id, apply=True)
    await review.refreshcaptions_cmd(apply, session, bot)
    await session.refresh(item)

    assert item.raw_caption == (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
    )
    assert item.lecture_number == 8
    assert item.status == "classified"
    apply_text = _answer_text(apply)
    assert "1 reclassified" in apply_text
    assert "[classified] L8 Liver Kidney" in apply_text


async def test_refresh_preview_and_apply_populate_missing_pipe_number_from_source(
    session, pipe_numbered_catalogue
):
    item = await _ingest(
        session,
        40,
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney",
    )
    assert item.status == "classified"
    assert item.lecture_number is None

    source_caption = (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
    )
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=900, caption=source_caption
    )
    bot.delete_message.return_value = True

    preview = _refresh_message(item.batch_id)
    await review.refreshcaptions_cmd(preview, session, bot)
    preview_text = _answer_text(preview)
    assert "action: update" in preview_text
    assert "current=L?" in preview_text
    assert "source=L8" in preview_text
    assert "proposed=L8" in preview_text

    await session.refresh(item)
    assert item.raw_caption == (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney"
    )
    assert item.lecture_number is None

    apply = _refresh_message(item.batch_id, apply=True)
    await review.refreshcaptions_cmd(apply, session, bot)
    await session.refresh(item)

    assert item.raw_caption == source_caption
    assert item.lecture_number == 8
    assert item.status == "classified"
    apply_text = _answer_text(apply)
    assert "1 updated" in apply_text
    assert "[classified] L8 Liver Kidney" in apply_text


async def test_refresh_preview_proposes_numbers_for_existing_abdomen_batch(
    session, pipe_numbered_catalogue
):
    items = []
    for message_id in range(50, 58):
        number = message_id - 49
        title = f"Abdomen Lesson {number}"
        item = await _ingest(
            session,
            message_id,
            f"Anatomy | Dr Mohammad Azam Sir | Abdomen | {title} | L{number}",
        )
        item.lecture_number = None
        await session.commit()
        items.append(item)

    bot = AsyncMock()
    bot.forward_message.side_effect = TelegramBadRequest(
        None, "message to forward not found"
    )

    preview = _refresh_message(items[0].batch_id)
    await review.refreshcaptions_cmd(preview, session, bot)
    preview_text = _answer_text(preview)

    for number in range(1, 9):
        assert f"proposed=L{number}" in preview_text

    for item in items:
        await session.refresh(item)
        assert item.lecture_number is None
        assert item.status == "classified"


async def test_refresh_does_not_apply_pipe_number_to_approved_batch(
    session, pipe_numbered_catalogue
):
    item = await _ingest(
        session,
        60,
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8",
    )
    item.lecture_number = None
    await session.commit()

    ok, text = await approve_batch(session, item.batch_id, 123)
    assert ok is True, text

    bot = AsyncMock()
    message = _refresh_message(item.batch_id, apply=True)
    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
    )
    assert item.lecture_number is None
    assert "approved" in _answer_text(message)
    bot.forward_message.assert_not_awaited()


def test_student_title_uses_numbered_pipe_video_title():
    raw_caption = "Anatomy | Dr A | Abdomen | Liver Kidney | L8"
    assert (
        resolve_lecture_title(
            "Lecture",
            subject_name="Anatomy",
            raw_caption=raw_caption,
        )
        == "Liver Kidney"
    )

    no_number = "Anatomy | Dr A | Abdomen | Liver Kidney"
    assert (
        resolve_lecture_title(
            "Lecture",
            subject_name="Anatomy",
            raw_caption=no_number,
        )
        == "Liver Kidney"
    )

    legacy = "Anatomy | Abdomen | L8 | Dr A"
    assert (
        resolve_lecture_title(
            "Lecture",
            subject_name="Anatomy",
            raw_caption=legacy,
        )
        == "Abdomen"
    )
