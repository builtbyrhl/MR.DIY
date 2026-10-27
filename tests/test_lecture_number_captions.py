from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest

from bot.db.ingestion_repo import approve_batch, get_batch_items
from bot.db.models import Course, Subject
from bot.db.repo import (
    add_teacher,
    add_topic,
    list_item_topics,
    list_lectures_by_topic,
)
from bot.handlers import review
from bot.services.ingestion import ingest_media, parse_caption
from bot.services.publishing import publish_batch
from bot.student_ui import resolve_lecture_title


@pytest_asyncio.fixture
async def numbered_catalogue(session):
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
    skull, error = await add_topic(session, anatomy.id, "Skull")
    assert error is None
    thorax, error = await add_topic(session, anatomy.id, "Thorax")
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
        skull=skull,
        thorax=thorax,
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


def test_numbered_slash_caption_parses_only_the_final_number_field():
    for caption, number, title in (
        ("Anatomy / Dr A / Thorax / Thoracic wall / L1", 1, "Thoracic wall"),
        ("Anatomy / Dr A / Thorax / Intercostal Artery / L2", 2, "Intercostal Artery"),
        ("Anatomy / Dr A / Thorax / Coronary Circulation / l10", 10, "Coronary Circulation"),
    ):
        parsed = parse_caption(caption, None)
        assert parsed.structured is True
        assert parsed.caption_format == "slash_numbered"
        assert parsed.subject_name == "Anatomy"
        assert parsed.teacher_name == "Dr A"
        assert parsed.topic_name == "Thorax"
        assert parsed.lecture_number == number
        assert parsed.numbers == [number]
        assert parsed.lecture_title == title
        assert parsed.title == title
        assert parsed.number_required is True
        assert parsed.format_error is None

    titled = parse_caption(
        "Anatomy / Dr A / Thorax / L2 Overview / L3", None
    )
    assert titled.lecture_number == 3
    assert titled.numbers == [3]
    assert titled.lecture_title == "L2 Overview"
    assert titled.title == "L2 Overview"


def test_numbered_slash_caption_rejects_invalid_numbers_without_losing_title():
    for value in ("L0", "L1000", "L2 extra", "LL2", "Lecture 2"):
        parsed = parse_caption(
            f"Anatomy / Dr A / Thorax / Thoracic wall / {value}", None
        )
        assert parsed.structured is True
        assert parsed.caption_format == "slash_numbered"
        assert parsed.lecture_number is None
        assert parsed.numbers == []
        assert parsed.title == "Thoracic wall"
        assert parsed.format_error == f"invalid lecture number: {value}"


def test_four_field_captions_without_a_final_number_keep_legacy_behavior():
    unnumbered = parse_caption(
        "Anatomy / Dr A / Thorax / Thoracic wall", None
    )
    assert unnumbered.caption_format == "slash_preferred"
    assert unnumbered.lecture_number is None
    assert unnumbered.numbers == []
    assert unnumbered.title == "Thoracic wall"

    title_number = parse_caption(
        "Anatomy / Dr A / Thorax / L2 vertebra compression", None
    )
    assert title_number.caption_format == "slash_preferred"
    assert title_number.lecture_number is None
    assert title_number.numbers == []
    assert title_number.title == "L2 vertebra compression"


async def test_numbered_caption_stores_number_and_resolves_metadata(
    session, numbered_catalogue
):
    cat = numbered_catalogue
    item = await _ingest(
        session,
        1,
        "Anatomy / Dr Mohammad Azam Sir / Thorax / Thoracic wall / L1",
    )
    assert item.status == "classified"
    assert item.course_id == cat.anatomy_course.id
    assert item.teacher_id == cat.anatomy_teacher.id
    assert item.lecture_number == 1
    assert item.title == "Thoracic wall"
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.thorax.id
    ]

    invalid = await _ingest(
        session,
        2,
        "Anatomy / Dr Mohammad Azam Sir / Thorax / Intercostal Artery / L0",
    )
    assert invalid.status == "needs_review"
    assert invalid.lecture_number is None
    assert "invalid lecture number: L0" in invalid.review_reason
    assert [topic.id for topic in await list_item_topics(session, invalid.id)] == [
        cat.thorax.id
    ]


async def test_duplicate_lecture_number_is_scoped_to_course_and_topic(
    session, numbered_catalogue
):
    cat = numbered_catalogue
    first = await _ingest(
        session,
        10,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem / L1",
    )
    duplicate = await _ingest(
        session,
        11,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Cranial Nerves / L1",
    )
    other_topic = await _ingest(
        session,
        12,
        "Anatomy / Dr Mohammad Azam Sir / Thorax / Thoracic wall / L1",
    )
    other_course = await _ingest(
        session,
        13,
        "Physiology / Dr Physio / Brain / Brainstem / L1",
    )

    assert first.status == "classified"
    assert duplicate.status == "needs_review"
    assert duplicate.lecture_number == 1
    assert "duplicate lecture number L1" in duplicate.review_reason
    assert f"item #{first.id}" in duplicate.review_reason
    assert other_topic.status == "classified"
    assert other_course.status == "classified"


async def test_numbered_captions_sort_numerically_after_shuffled_uploads(
    session, numbered_catalogue
):
    cat = numbered_catalogue
    for message_id, number, title in (
        (20, 10, "Tenth"),
        (21, 2, "Second"),
        (22, 1, "First"),
    ):
        await _ingest(
            session,
            message_id,
            "Anatomy / Dr Mohammad Azam Sir / Skull / "
            f"{title} / L{number}",
        )

    items = await get_batch_items(session, 1)
    assert [(item.lecture_number, item.title) for item in items] == [
        (1, "First"),
        (2, "Second"),
        (10, "Tenth"),
    ]
    assert [topic.id for topic in await list_item_topics(session, items[0].id)] == [
        cat.skull.id
    ]


async def test_publishing_and_catalogue_order_follow_numeric_lecture_numbers(
    session, numbered_catalogue
):
    cat = numbered_catalogue
    items = []
    for message_id, number, title in (
        (30, 10, "Tenth"),
        (31, 2, "Second"),
        (32, 1, "First"),
    ):
        item = await _ingest(
            session,
            message_id,
            "Anatomy / Dr Mohammad Azam Sir / Skull / "
            f"{title} / L{number}",
        )
        items.append(item)

    cat.anatomy_course.destination_chat_id = "-100123"
    cat.anatomy_course.topic_id = 42
    await session.commit()
    ok, text = await approve_batch(session, items[0].batch_id, 123)
    assert ok is True, text

    bot = AsyncMock()
    bot.send_video.side_effect = [
        SimpleNamespace(message_id=500),
        SimpleNamespace(message_id=501),
        SimpleNamespace(message_id=502),
    ]
    result = await publish_batch(session, bot, items[0].batch_id)

    assert result.published == 3
    captions = [
        call.kwargs["caption"] for call in bot.send_video.await_args_list
    ]
    assert captions == [
        "Anatomy / Dr Mohammad Azam Sir / Skull / First / L1",
        "Anatomy / Dr Mohammad Azam Sir / Skull / Second / L2",
        "Anatomy / Dr Mohammad Azam Sir / Skull / Tenth / L10",
    ]

    lectures = await list_lectures_by_topic(
        session, cat.anatomy.id, cat.skull.id
    )
    assert [(lecture.order, lecture.title) for lecture in lectures] == [
        (1, "First"),
        (2, "Second"),
        (10, "Tenth"),
    ]


async def test_refresh_preview_and_apply_populate_missing_number(
    session, numbered_catalogue
):
    item = await _ingest(
        session,
        40,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem",
    )
    assert item.status == "classified"
    assert item.lecture_number is None

    source_caption = (
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem / L2"
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
    assert "source=L2" in preview_text
    assert "proposed=L2" in preview_text

    await session.refresh(item)
    assert item.raw_caption == (
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem"
    )
    assert item.lecture_number is None

    apply = _refresh_message(item.batch_id, apply=True)
    await review.refreshcaptions_cmd(apply, session, bot)
    await session.refresh(item)

    assert item.raw_caption == source_caption
    assert item.lecture_number == 2
    assert item.status == "classified"
    apply_text = _answer_text(apply)
    assert "1 updated" in apply_text
    assert "[classified] L2 Brainstem" in apply_text


async def test_refresh_reclassifies_wrong_number_from_valid_caption(
    session, numbered_catalogue
):
    item = await _ingest(
        session,
        50,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem / L2",
    )
    item.lecture_number = 7
    await session.commit()

    bot = AsyncMock()
    bot.forward_message.side_effect = TelegramBadRequest(
        None, "message to forward not found"
    )
    message = _refresh_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)
    await session.refresh(item)

    assert item.raw_caption == (
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem / L2"
    )
    assert item.lecture_number == 2
    assert item.status == "classified"
    assert "1 reclassified" in _answer_text(message)


async def test_refresh_does_not_apply_numbered_source_to_approved_batch(
    session, numbered_catalogue
):
    item = await _ingest(
        session,
        60,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem",
    )
    ok, text = await approve_batch(session, item.batch_id, 123)
    assert ok is True, text

    bot = AsyncMock()
    message = _refresh_message(item.batch_id, apply=True)
    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == (
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem"
    )
    assert item.lecture_number is None
    assert "approved" in _answer_text(message)
    bot.forward_message.assert_not_awaited()


def test_student_title_uses_numbered_slash_video_title():
    raw_caption = "Anatomy / Dr A / Thorax / Thoracic wall / L1"
    assert (
        resolve_lecture_title(
            "Anatomy",
            subject_name="Anatomy",
            raw_caption=raw_caption,
        )
        == "Thoracic wall"
    )

    title_with_number = "Anatomy / Dr A / Thorax / L2 Overview / L3"
    assert (
        resolve_lecture_title(
            "Anatomy",
            subject_name="Anatomy",
            raw_caption=title_with_number,
        )
        == "L2 Overview"
    )
