from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Course, Subject, Topic
from bot.db.repo import (
    add_teacher,
    add_topic,
    find_matching_topics,
    list_item_topics,
)
from bot.handlers import review
from bot.services.ingestion import ingest_media


@pytest_asyncio.fixture
async def anatomy_topics(session: AsyncSession):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="🫀", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    course = Course(
        year="",
        subject_id=subject.id,
        name="Anatomy",
        aliases="anatomy",
        topic_id=6,
    )
    session.add(course)
    await session.commit()
    await session.refresh(course)

    teacher, error = await add_teacher(session, subject.id, "Dr A")
    assert error is None

    neuro, error = await add_topic(session, subject.id, "Neuroanatomy")
    assert error is None
    thorax, error = await add_topic(session, subject.id, "Thorax")
    assert error is None
    head_neck, error = await add_topic(session, subject.id, "Head and neck")
    assert error is None
    abdomen, error = await add_topic(session, subject.id, "Abdomen")
    assert error is None
    upper_limb, error = await add_topic(session, subject.id, "Upper Limb")
    assert error is None

    return SimpleNamespace(
        subject=subject,
        course=course,
        teacher=teacher,
        neuro=neuro,
        thorax=thorax,
        head_neck=head_neck,
        abdomen=abdomen,
        upper_limb=upper_limb,
    )


async def _ingest(
    session: AsyncSession, message_id: int, caption: str
) -> object:
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


def _refresh_message(batch_id: int, *, apply: bool = False):
    suffix = " apply" if apply else ""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/refreshcaptions {batch_id}{suffix}",
        answer=AsyncMock(),
    )


def _answer_text(message):
    return "\n".join(call.args[0] for call in message.answer.await_args_list)


async def test_find_matching_topics_handles_case_and_spacing_variations(
    session, anatomy_topics
):
    cat = anatomy_topics
    for variant in (
        "Upper Limb",
        "upper limb",
        "UPPER   LIMB",
        "Upper-Limb",
        "upper.limb",
    ):
        matches = await find_matching_topics(session, cat.subject.id, variant)
        assert [topic.id for topic in matches] == [cat.upper_limb.id]


async def test_find_matching_topics_recovers_stale_normalized_name(session):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="🫀", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    stale_topic = Topic(
        subject_id=subject.id,
        name="Upper Limb",
        normalized_name="upper limb ",
    )
    session.add(stale_topic)
    await session.commit()
    await session.refresh(stale_topic)

    matches = await find_matching_topics(session, subject.id, "Upper Limb")
    assert [topic.id for topic in matches] == [stale_topic.id]

    duplicate, error = await add_topic(session, subject.id, "Upper Limb")
    assert duplicate is None
    assert error == (
        f"Topic already exists: #{stale_topic.id} {stale_topic.name}."
    )


async def test_course_telegram_topic_id_does_not_shadow_catalogue_topic(
    session, anatomy_topics
):
    cat = anatomy_topics
    assert cat.course.topic_id == 6
    assert cat.upper_limb.id != 6

    item = await _ingest(
        session,
        1,
        "Anatomy | Dr A | Upper Limb | Brachial Plexus | L1",
    )

    assert item.status == "classified"
    assert item.course_id == cat.course.id
    assert item.lecture_number == 1
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.upper_limb.id
    ]


async def test_refresh_reclassifies_stale_upper_limb_review_state(
    session, anatomy_topics
):
    cat = anatomy_topics
    item = await _ingest(
        session,
        2,
        "Anatomy | Dr A | Upper Limb | Brachial Plexus | L1",
    )
    assert item.status == "classified"

    item.status = "needs_review"
    item.review_reason = "topic not found: Upper Limb"
    item.topic_review_reason = "topic not found: Upper Limb"
    item.topic_source = None
    from bot.db.repo import replace_item_topics

    await replace_item_topics(session, item.id, [])
    await session.commit()

    bot = AsyncMock()
    bot.forward_message.side_effect = TelegramBadRequest(
        None, "message to forward not found"
    )
    message = _refresh_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)
    await session.refresh(item)

    assert item.raw_caption == "Anatomy | Dr A | Upper Limb | Brachial Plexus | L1"
    assert item.lecture_number == 1
    assert item.status == "classified"
    assert item.review_reason is None
    assert item.topic_review_reason is None
    assert item.topic_source == "auto"
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.upper_limb.id
    ]
    assert "1 reclassified" in _answer_text(message)


async def test_existing_anatomy_topics_continue_to_resolve(session, anatomy_topics):
    cat = anatomy_topics
    captions = [
        (3, "Neuroanatomy", cat.neuro.id),
        (4, "Thorax", cat.thorax.id),
        (5, "Head and neck", cat.head_neck.id),
        (6, "Abdomen", cat.abdomen.id),
        (7, "Upper Limb", cat.upper_limb.id),
    ]

    for index, (message_id, topic_name, expected_topic_id) in enumerate(captions, 1):
        item = await _ingest(
            session,
            message_id,
            f"Anatomy | Dr A | {topic_name} | Title {index} | L{index}",
        )
        assert item.status == "classified", item.review_reason
        assert item.lecture_number == index
        assert [
            topic.id for topic in await list_item_topics(session, item.id)
        ] == [expected_topic_id]
