from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from bot.db.ingestion_repo import (
    approve_batch,
    set_item_course,
    set_item_topics,
)
from bot.db.models import Course, Lecture, Subject, User
from bot.db.repo import (
    add_teacher,
    add_teacher_alias,
    add_topic,
    find_matching_topics,
    list_item_topics,
    list_lecture_topics,
    list_lectures_by_topic,
    list_subject_topic_groups,
    list_topics,
    set_lecture_title,
)
from bot.errors import ApprovedBatchError
from bot.handlers import review
from bot.handlers.lectures import open_lecture
from bot.handlers.subjects import (
    back_teacher_topic,
    back_topic,
    open_subject,
    open_teacher_lecture_list,
    open_teacher_lectures,
    open_teacher_topic_lectures,
    open_topic_lectures,
    topics_page,
)
from bot.services.access import APPROVED
from bot.services.catalog import sync_published_uploads_to_catalog
from bot.student_ui import lecture_label, lecture_title
from bot.services.ingestion import (
    ingest_media,
    parse_caption,
    retry_item,
    set_item_caption,
    update_ingested_caption,
)


@pytest_asyncio.fixture
async def topic_catalogue(session):
    anatomy = Subject(slug="anatomy", name="Anatomy", emoji="🫀", order=1)
    physiology = Subject(slug="physiology", name="Physiology", emoji="⚡", order=2)
    session.add_all([anatomy, physiology])
    await session.commit()
    await session.refresh(anatomy)
    await session.refresh(physiology)

    anatomy_course = Course(
        year="",
        subject_id=anatomy.id,
        name="Anatomy",
        aliases="anatomy",
    )
    physiology_course = Course(
        year="",
        subject_id=physiology.id,
        name="Physiology",
        aliases="physiology",
    )
    session.add_all([anatomy_course, physiology_course])
    teacher, error = await add_teacher(session, anatomy.id, "Dr Mohammad Azam Sir")
    assert error is None
    skull, error = await add_topic(session, anatomy.id, "Skull")
    assert error is None
    upper_limb, error = await add_topic(session, anatomy.id, "Upper Limb")
    assert error is None
    brain, error = await add_topic(session, physiology.id, "Brain")
    assert error is None
    return SimpleNamespace(
        anatomy=anatomy,
        physiology=physiology,
        anatomy_course=anatomy_course,
        physiology_course=physiology_course,
        teacher=teacher,
        skull=skull,
        upper_limb=upper_limb,
        brain=brain,
    )


async def _ingest(session, message_id, caption, file_id=None):
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type="video",
        caption=caption,
        file_name=None,
        file_id=file_id or f"file-{message_id}",
        file_unique_id=f"unique-{message_id}",
    )
    return item


def _callback(data, user_id=9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _button_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _button_labels(markup):
    return [
        button.text
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def test_structured_caption_parsing_is_strict_and_backward_compatible():
    parsed = parse_caption(
        "Anatomy | Skull | Lecture 1 | Dr Mohammad Azam Sir", "video.mp4"
    )
    assert parsed.structured is True
    assert parsed.subject_name == "Anatomy"
    assert parsed.topic_name == "Skull"
    assert parsed.title == "Skull"
    assert parsed.lecture_number == 1
    assert parsed.teacher_name == "Dr Mohammad Azam Sir"
    assert parsed.teacher_missing is False

    missing_teacher = parse_caption("Anatomy | Skull | Lecture 1", None)
    assert missing_teacher.structured is True
    assert missing_teacher.teacher_name is None
    assert missing_teacher.teacher_missing is True

    legacy = parse_caption(
        "Lecture 10: Pathophysiology | Dr Sharma", "lecture.mp4"
    )
    assert legacy.structured is False
    assert legacy.topic_name is None
    assert legacy.lecture_number == 10

    non_numeric_third_field = parse_caption(
        "Anatomy | Skull | Dr Azam", None
    )
    assert non_numeric_third_field.structured is False


def test_slash_caption_parsing_supports_both_layouts():
    preferred = parse_caption(
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem", None
    )
    assert preferred.structured is True
    assert preferred.caption_format == "slash_preferred"
    assert preferred.subject_name == "Anatomy"
    assert preferred.teacher_name == "Dr Mohammad Azam Sir"
    assert preferred.topic_name == "Skull"
    assert preferred.lecture_number is None
    assert preferred.numbers == []
    assert preferred.number_required is False
    assert preferred.lecture_title == "Brainstem"
    assert preferred.title == "Brainstem"
    assert preferred.teacher_missing is False

    legacy = parse_caption(
        "Anatomy / Skull / Lecture 12 / Dr Mohammad Azam Sir", None
    )
    assert legacy.structured is True
    assert legacy.caption_format == "slash_legacy"
    assert legacy.subject_name == "Anatomy"
    assert legacy.teacher_name == "Dr Mohammad Azam Sir"
    assert legacy.topic_name == "Skull"
    assert legacy.lecture_number == 12
    assert legacy.numbers == [12]
    assert legacy.number_required is True

    titled = parse_caption(
        "Anatomy / Skull / Lecture 3: Cranial Nerves / Dr Mohammad Azam Sir",
        None,
    )
    assert titled.lecture_number == 3
    assert titled.lecture_title == "Cranial Nerves"
    assert titled.title == "Cranial Nerves"


def test_slash_caption_parsing_rejects_ambiguous_or_invalid_layouts():
    ambiguous = parse_caption(
        "Anatomy / Skull / Lecture 1 / Lecture 2", None
    )
    assert ambiguous.structured is True
    assert ambiguous.ambiguous is True
    assert ambiguous.number_ambiguous is True
    assert ambiguous.lecture_number is None
    assert len(ambiguous.slash_candidates) == 2

    teacher_ambiguous = parse_caption(
        "Anatomy / Dr A / Skull / Dr B", None
    )
    assert teacher_ambiguous.ambiguous is True
    assert len(teacher_ambiguous.slash_candidates) == 2

    missing_title = parse_caption(
        "Anatomy / Dr Mohammad Azam Sir / Skull /", None
    )
    assert missing_title.structured is True
    assert missing_title.format_error == "video title missing"

    generic_title = parse_caption(
        "Anatomy / Dr Mohammad Azam Sir / Skull / Lecture 7", None
    )
    assert generic_title.format_error == "generic video title: Lecture 7"

    topic_as_title = parse_caption(
        "Anatomy / Dr Mohammad Azam Sir / Skull / Skull", None
    )
    assert topic_as_title.format_error == "generic video title: Skull"

    legacy_invalid = parse_caption(
        "Anatomy / Skull / Lecture X / Dr Mohammad Azam Sir", None
    )
    assert legacy_invalid.format_error == "invalid lecture number: Lecture X"
    assert legacy_invalid.teacher_name == "Dr Mohammad Azam Sir"

    missing_number = parse_caption(
        "Anatomy / Skull / Revision / Dr Mohammad Azam Sir", None
    )
    assert missing_number.format_error == "lecture number missing"


async def test_topics_are_subject_scoped_and_normalized(session, topic_catalogue):
    cat = topic_catalogue
    topics = await list_topics(session, cat.anatomy.id)
    assert [topic.name for topic in topics] == ["Skull", "Upper Limb"]

    matches = await find_matching_topics(session, cat.anatomy.id, "upper-limb")
    assert [topic.id for topic in matches] == [cat.upper_limb.id]

    other_matches = await find_matching_topics(session, cat.physiology.id, "Skull")
    assert other_matches == []

    duplicate, error = await add_topic(session, cat.anatomy.id, "upper limb")
    assert duplicate is None
    assert "already exists" in error

    other_subject, error = await add_topic(session, cat.physiology.id, "Skull")
    assert error is None
    assert other_subject.subject_id == cat.physiology.id


async def test_new_caption_assigns_topic_and_reports_missing_or_unknown_values(
    session, topic_catalogue
):
    cat = topic_catalogue
    skull = await _ingest(
        session,
        1,
        "Anatomy | Skull | Lecture 1 | Dr Mohammad Azam Sir",
    )
    assert skull.course_id == cat.anatomy_course.id
    assert skull.teacher_id == cat.teacher.id
    assert skull.topic_source == "auto"
    assert skull.title == "Skull"
    assert [topic.id for topic in await list_item_topics(session, skull.id)] == [
        cat.skull.id
    ]
    assert skull.status == "classified"

    unknown_topic = await _ingest(
        session,
        2,
        "Anatomy | Neuroanatomy | Lecture 2 | Dr Mohammad Azam Sir",
    )
    assert unknown_topic.status == "needs_review"
    assert "topic not found: Neuroanatomy" in unknown_topic.review_reason
    assert await list_item_topics(session, unknown_topic.id) == []

    missing_teacher = await _ingest(
        session,
        3,
        "Anatomy | Skull | Lecture 3",
    )
    assert missing_teacher.status == "needs_review"
    assert "teacher missing" in missing_teacher.review_reason
    assert [topic.id for topic in await list_item_topics(session, missing_teacher.id)] == [
        cat.skull.id
    ]

    legacy = await _ingest(
        session,
        4,
        "Anatomy Lecture 4 - Skull | Dr Mohammad Azam Sir",
    )
    assert legacy.status == "classified"
    assert await list_item_topics(session, legacy.id) == []


async def test_slash_captions_classify_with_existing_catalogue(
    session, topic_catalogue
):
    cat = topic_catalogue
    preferred = await _ingest(
        session,
        41,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem",
    )
    assert preferred.status == "classified"
    assert preferred.course_id == cat.anatomy_course.id
    assert preferred.teacher_id == cat.teacher.id
    assert preferred.lecture_number is None
    assert preferred.title == "Brainstem"
    assert [topic.id for topic in await list_item_topics(session, preferred.id)] == [
        cat.skull.id
    ]

    _, error = await add_teacher_alias(session, cat.teacher.id, "Dr M Azam")
    assert error is None
    aliased = await _ingest(
        session,
        42,
        "Anatomy / Dr M Azam / Skull / Pons",
    )
    assert aliased.status == "classified"
    assert aliased.teacher_id == cat.teacher.id
    assert aliased.title == "Pons"

    legacy = await _ingest(
        session,
        43,
        "Anatomy / Skull / Lecture 12 / Dr Mohammad Azam Sir",
    )
    assert legacy.status == "classified"
    assert legacy.teacher_id == cat.teacher.id
    assert legacy.lecture_number == 12
    assert legacy.title == "Skull"
    assert [topic.id for topic in await list_item_topics(session, legacy.id)] == [
        cat.skull.id
    ]

    unknown_topic = await _ingest(
        session,
        44,
        "Anatomy / Dr Mohammad Azam Sir / Neuroanatomy / Brainstem",
    )
    assert unknown_topic.status == "needs_review"
    assert "topic not found: Neuroanatomy" in unknown_topic.review_reason
    assert await list_item_topics(session, unknown_topic.id) == []

    missing_title = await _ingest(
        session,
        45,
        "Anatomy / Dr Mohammad Azam Sir / Skull / ",
    )
    assert missing_title.status == "needs_review"
    assert "video title missing" in missing_title.review_reason

    generic_title = await _ingest(
        session,
        40,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Lecture 7",
    )
    assert generic_title.status == "needs_review"
    assert "generic video title: Lecture 7" in generic_title.review_reason

    invalid_number = await _ingest(
        session,
        46,
        "Anatomy / Skull / Lecture X / Dr Mohammad Azam Sir",
    )
    assert invalid_number.status == "needs_review"
    assert "invalid lecture number: Lecture X" in invalid_number.review_reason

    resolved = await _ingest(
        session,
        47,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Dr Guest",
    )
    assert resolved.status == "classified"
    assert resolved.teacher_id == cat.teacher.id
    assert resolved.title == "Dr Guest"
    assert [topic.id for topic in await list_item_topics(session, resolved.id)] == [
        cat.skull.id
    ]

    ambiguous = await _ingest(
        session,
        48,
        "Anatomy / Skull / Lecture 1 / Lecture 2",
    )
    assert ambiguous.status == "needs_review"
    assert "ambiguous slash caption format" in ambiguous.review_reason


async def test_published_title_correction_keeps_media_and_caption(
    session, topic_catalogue
):
    cat = topic_catalogue
    item = await _ingest(
        session,
        49,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem",
    )
    item.publication_status = "published"
    item.published_message_id = 490
    await session.commit()
    assert await sync_published_uploads_to_catalog(session) == 1
    lecture = await session.scalar(
        select(Lecture).where(Lecture.source_upload_item_id == item.id)
    )
    assert lecture is not None
    assert lecture.title == "Brainstem"
    assert lecture_title(lecture) == "Brainstem"
    assert "Brainstem" in lecture_label(lecture)
    assert "Anatomy" not in lecture_title(lecture)
    raw_caption = item.raw_caption
    file_id = item.file_id

    lecture, error = await set_lecture_title(
        session, lecture.id, "Medulla Oblongata"
    )
    assert error is None
    assert lecture.title == "Medulla Oblongata"
    await session.refresh(item)
    assert item.title == "Medulla Oblongata"
    assert item.raw_caption == raw_caption
    assert item.file_id == file_id
    assert item.publication_status == "published"

    await session.commit()
    assert await sync_published_uploads_to_catalog(session) == 0
    await session.refresh(lecture)
    assert lecture.title == "Medulla Oblongata"


async def test_manual_topic_assignment_and_clear_survive_retry(
    session, topic_catalogue
):
    cat = topic_catalogue
    item = await _ingest(
        session,
        5,
        "Anatomy | Missing Topic | Lecture 5 | Dr Mohammad Azam Sir",
    )
    assert item.status == "needs_review"

    item, error = await set_item_topics(
        session, item.id, [cat.skull.id, cat.upper_limb.id]
    )
    assert error is None
    assert item.topic_source == "manual"
    assert item.status == "classified"
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.skull.id,
        cat.upper_limb.id,
    ]

    item = await retry_item(session, item.id)
    assert item.topic_source == "manual"
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.skull.id,
        cat.upper_limb.id,
    ]

    item, error = await set_item_topics(session, item.id, [])
    assert error is None
    assert item.topic_source == "manual"
    assert await list_item_topics(session, item.id) == []

    item = await retry_item(session, item.id)
    assert item.topic_source == "manual"
    assert await list_item_topics(session, item.id) == []


async def test_editing_caption_reclassifies_and_preserves_original(
    session, topic_catalogue
):
    cat = topic_catalogue
    item = await _ingest(
        session,
        6,
        "Lecture 6: Anatomy - Cranial Nerves",
    )
    original_caption = item.raw_caption
    assert await list_item_topics(session, item.id) == []

    item, error = await set_item_caption(
        session,
        item.id,
        "Anatomy | Skull | Lecture 1 | Dr Mohammad Azam Sir",
    )
    assert error is None
    assert item.original_caption == original_caption
    assert item.title == "Skull"
    assert item.lecture_number == 1
    assert item.teacher_id == cat.teacher.id
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.skull.id
    ]
    assert item.status == "classified"

    ok, message = await approve_batch(session, item.batch_id, 123)
    assert ok is True, message
    with pytest.raises(ApprovedBatchError):
        await set_item_caption(
            session,
            item.id,
            "Anatomy | Upper Limb | Lecture 2 | Dr Mohammad Azam Sir",
        )


async def test_source_caption_edit_reclassifies_topic_and_keeps_original(
    session, topic_catalogue
):
    cat = topic_catalogue
    item = await _ingest(
        session,
        15,
        "Anatomy | Skull | Lecture 1 | Dr Mohammad Azam Sir",
    )
    original_caption = item.raw_caption

    item, outcome = await update_ingested_caption(
        session,
        source_chat_id=-1001,
        source_message_id=15,
        caption="Anatomy | Upper Limb | Lecture 2 | Dr Mohammad Azam Sir",
    )

    assert outcome == "updated"
    assert item.raw_caption == (
        "Anatomy | Upper Limb | Lecture 2 | Dr Mohammad Azam Sir"
    )
    assert item.original_caption == original_caption
    assert item.title == "Upper Limb"
    assert item.lecture_number == 2
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.upper_limb.id
    ]


async def test_publish_projects_per_topic_lectures_without_unique_numbers(
    session, topic_catalogue
):
    cat = topic_catalogue
    items = [
        await _ingest(
            session,
            7,
            "Anatomy | Skull | Lecture 1 | Dr Mohammad Azam Sir",
        ),
        await _ingest(
            session,
            8,
            "Anatomy | Skull | Lecture 2 | Dr Mohammad Azam Sir",
        ),
        await _ingest(
            session,
            9,
            "Anatomy | Upper Limb | Lecture 1 | Dr Mohammad Azam Sir",
        ),
    ]
    for offset, item in enumerate(items, start=1):
        item.publication_status = "published"
        item.published_message_id = 500 + offset
    await session.commit()

    assert await sync_published_uploads_to_catalog(session) == 3

    skull_lectures = await list_lectures_by_topic(
        session, cat.anatomy.id, cat.skull.id
    )
    upper_lectures = await list_lectures_by_topic(
        session, cat.anatomy.id, cat.upper_limb.id
    )
    assert [lecture.order for lecture in skull_lectures] == [1, 2]
    assert [lecture.order for lecture in upper_lectures] == [1]
    assert len(skull_lectures) + len(upper_lectures) == 3

    groups = await list_subject_topic_groups(session, cat.anatomy.id)
    assert [(group.name, group.lecture_count) for group in groups] == [
        ("Skull", 2),
        ("Upper Limb", 1),
    ]
    assert await list_subject_topic_groups(session, cat.physiology.id) == []


async def test_subject_move_clears_item_and_lecture_topics(
    session, topic_catalogue
):
    cat = topic_catalogue
    item = await _ingest(
        session,
        10,
        "Anatomy | Skull | Lecture 1 | Dr Mohammad Azam Sir",
    )
    item.publication_status = "published"
    item.published_message_id = 900
    await session.commit()
    assert await sync_published_uploads_to_catalog(session) == 1

    lecture = await session.scalar(
        select(Lecture).where(Lecture.source_upload_item_id == item.id)
    )
    assert [topic.id for topic in await list_lecture_topics(session, lecture.id)] == [
        cat.skull.id
    ]

    item, error = await set_item_course(
        session, item.id, cat.physiology_course.id
    )
    assert error is None
    assert item.course_id == cat.physiology_course.id
    assert item.topic_source is None
    assert await list_item_topics(session, item.id) == []
    assert await list_lecture_topics(session, lecture.id) == []


async def test_student_topic_browsing_and_lecture_back_navigation(
    session, topic_catalogue
):
    cat = topic_catalogue
    user = User(
        id=9001,
        username="student",
        full_name="Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )
    session.add(user)
    item = await _ingest(
        session,
        11,
        "Anatomy / Dr Mohammad Azam Sir / Skull / Brainstem",
    )
    item.publication_status = "published"
    item.published_message_id = 901
    await session.commit()
    await sync_published_uploads_to_catalog(session)
    lecture = await session.scalar(select(Lecture))
    assert lecture is not None
    assert lecture.title == "Brainstem"

    cb = _callback(f"subj:{cat.anatomy.slug}", user.id)
    await open_subject(cb, session, user)
    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert f"tlec:{cat.anatomy.id}:{cat.teacher.id}:0" in data
    assert f"topics:{cat.anatomy.id}:0" in data

    cb = _callback(
        f"tlec:{cat.anatomy.id}:{cat.teacher.id}:0", user.id
    )
    await open_teacher_lectures(cb, session, user)
    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    topic_callback = (
        f"ttopic:{cat.anatomy.id}:{cat.teacher.id}:{cat.skull.id}:0"
    )
    assert topic_callback in data
    assert f"tlecflat:{cat.anatomy.id}:{cat.teacher.id}:0" in data

    cb = _callback(topic_callback, user.id)
    await open_teacher_topic_lectures(cb, session, user)
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    data = _button_data(markup)
    assert any("Brainstem" in label for label in _button_labels(markup))
    assert not any(label == "Anatomy" for label in _button_labels(markup))
    lecture_callback = next(value for value in data if value.startswith("lec:"))
    assert lecture_callback == (
        f"lec:{lecture.id}:tcht:{cat.anatomy.id}:{cat.teacher.id}:"
        f"{cat.skull.id}:0"
    )

    cb = _callback(lecture_callback, user.id)
    await open_lecture(cb, session, user, AsyncMock())
    back_data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert (
        f"back_teacher_topic:{cat.anatomy.id}:{cat.teacher.id}:"
        f"{cat.skull.id}:0"
    ) in back_data

    cb = _callback(
        f"back_teacher_topic:{cat.anatomy.id}:{cat.teacher.id}:"
        f"{cat.skull.id}:0",
        user.id,
    )
    await back_teacher_topic(cb, session, user)
    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert lecture_callback in data
    cb.answer.assert_awaited_once_with()

    cb = _callback(f"topics:{cat.anatomy.id}:0", user.id)
    await topics_page(cb, session, user)
    text = cb.message.edit_text.await_args.args[0]
    data = _button_data(cb.message.edit_text.await_args.kwargs["reply_markup"])
    assert "Choose a topic to open its lectures." in text
    assert f"topicview:{cat.anatomy.id}:{cat.skull.id}:0" in data
    assert f"teachers:{cat.anatomy.id}:0" in data


async def test_generic_subject_titles_use_topic_and_caption_at_display_time(
    session, topic_catalogue
):
    cat = topic_catalogue
    user = User(
        id=9002,
        username="student2",
        full_name="Student Two",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )
    session.add(user)
    item = await _ingest(
        session,
        21,
        "Anatomy | Skull | Lecture 3 | Dr Mohammad Azam Sir",
    )
    item.publication_status = "published"
    item.published_message_id = 902
    await session.commit()
    await sync_published_uploads_to_catalog(session)
    lecture = await session.scalar(
        select(Lecture).where(Lecture.source_upload_item_id == item.id)
    )
    assert lecture is not None
    lecture.title = "Anatomy"
    lecture.order = 3
    lecture.teacher_id = cat.teacher.id
    bare = Lecture(
        subject_id=cat.anatomy.id,
        title="Anatomy",
        order=4,
        teacher_id=cat.teacher.id,
    )
    session.add(bare)
    await session.commit()

    cb = _callback(
        f"tlecflat:{cat.anatomy.id}:{cat.teacher.id}:0",
        user.id,
    )
    await open_teacher_lecture_list(cb, session, user)

    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    labels = [
        button.text
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("lec:")
    ]
    assert any("Skull" in label for label in labels)
    assert any("Lecture 4" in label for label in labels)
    assert not any("Anatomy" in label for label in labels)


async def test_topic_review_commands_assign_and_clear(session, topic_catalogue):
    cat = topic_catalogue
    item = await _ingest(
        session,
        12,
        "Anatomy | Missing Topic | Lecture 1 | Dr Mohammad Azam Sir",
    )

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/settopic {item.id}|{cat.skull.id},{cat.upper_limb.id}",
        answer=AsyncMock(),
    )
    await review.settopic_cmd(message, session)
    assert "topics set to" in message.answer.await_args.args[0]
    item = await session.get(type(item), item.id)
    assert [topic.id for topic in await list_item_topics(session, item.id)] == [
        cat.skull.id,
        cat.upper_limb.id,
    ]

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/assign {item.id}|-|none|{cat.teacher.id}",
        answer=AsyncMock(),
    )
    await review.assign_cmd(message, session)
    assert "assigned" in message.answer.await_args.args[0]
    await session.refresh(item)
    assert item.teacher_id == cat.teacher.id
    assert item.topic_source == "manual"
    assert await list_item_topics(session, item.id) == []


async def test_assign_rolls_back_every_item_when_any_item_fails(
    session, topic_catalogue
):
    cat = topic_catalogue
    first = await _ingest(
        session,
        13,
        "Anatomy | Skull | Lecture 1 | Dr Mohammad Azam Sir",
    )
    second = await _ingest(
        session,
        14,
        "Physiology | Brain | Lecture 1",
    )
    first_id, second_id = first.id, second.id

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=(
            f"/assign {first_id},{second_id}|-|"
            f"{cat.upper_limb.id}|{cat.teacher.id}"
        ),
        answer=AsyncMock(),
    )
    await review.assign_cmd(message, session)

    answer = message.answer.await_args.args[0]
    assert f"#{second_id}" in answer
    assert "No changes were applied." in answer

    first = await session.get(type(first), first_id)
    second = await session.get(type(second), second_id)
    assert [topic.id for topic in await list_item_topics(session, first.id)] == [
        cat.skull.id
    ]
    assert first.topic_source == "auto"
    assert [topic.id for topic in await list_item_topics(session, second.id)] == [
        cat.brain.id
    ]


def test_pipe_preferred_caption_parsing_supports_current_layout():
    parsed = parse_caption(
        "Anatomy | Dr Mohammad Azam Sir | Neuroanatomy | Introduction",
        None,
    )
    assert parsed.structured is True
    assert parsed.caption_format == "pipe_preferred"
    assert parsed.subject_name == "Anatomy"
    assert parsed.teacher_name == "Dr Mohammad Azam Sir"
    assert parsed.topic_name == "Neuroanatomy"
    assert parsed.lecture_number is None
    assert parsed.numbers == []
    assert parsed.number_required is False
    assert parsed.lecture_title == "Introduction"
    assert parsed.title == "Introduction"
    assert parsed.teacher_missing is False


def test_pipe_legacy_caption_parsing_supports_number_variants():
    for number in ("1", "L1", "Lecture 1"):
        parsed = parse_caption(
            f"Anatomy | Skull | {number} | Dr Mohammad Azam Sir",
            None,
        )
        assert parsed.structured is True
        assert parsed.caption_format == "pipe_legacy"
        assert parsed.subject_name == "Anatomy"
        assert parsed.teacher_name == "Dr Mohammad Azam Sir"
        assert parsed.topic_name == "Skull"
        assert parsed.lecture_number == 1
        assert parsed.numbers == [1]
        assert parsed.number_required is True
        assert parsed.title == "Skull"


async def test_pipe_formats_classify_using_teacher_records_and_aliases(
    session, topic_catalogue
):
    cat = topic_catalogue
    preferred = await _ingest(
        session,
        60,
        "Anatomy | Dr Mohammad Azam Sir | Skull | Brainstem",
    )
    assert preferred.status == "classified"
    assert preferred.teacher_id == cat.teacher.id
    assert preferred.title == "Brainstem"
    assert [topic.id for topic in await list_item_topics(session, preferred.id)] == [
        cat.skull.id
    ]

    legacy = await _ingest(
        session,
        61,
        "Anatomy | Skull | L1 | Dr Mohammad Azam Sir",
    )
    assert legacy.status == "classified"
    assert legacy.teacher_id == cat.teacher.id
    assert legacy.lecture_number == 1
    assert legacy.title == "Skull"

    _, error = await add_teacher_alias(session, cat.teacher.id, "Azam")
    assert error is None
    aliased = await _ingest(
        session,
        62,
        "Anatomy | Azam | Skull | Pons",
    )
    assert aliased.status == "classified"
    assert aliased.teacher_id == cat.teacher.id
    assert aliased.title == "Pons"


async def test_pipe_unknown_teachers_are_never_assigned(
    session, topic_catalogue
):
    preferred = await _ingest(
        session,
        63,
        "Anatomy | Dr Unknown | Skull | Brainstem",
    )
    assert preferred.status == "needs_review"
    assert preferred.teacher_id is None
    assert "teacher not found: Dr Unknown" in preferred.review_reason

    legacy = await _ingest(
        session,
        64,
        "Anatomy | Skull | Lecture 1 | Dr Unknown",
    )
    assert legacy.status == "needs_review"
    assert legacy.teacher_id is None
    assert legacy.lecture_number == 1
    assert "teacher not found: Dr Unknown" in legacy.review_reason

    ambiguous_teacher = await _ingest(
        session,
        65,
        "Anatomy | Unknown Person | Skull | Brainstem",
    )
    assert ambiguous_teacher.status == "needs_review"
    assert ambiguous_teacher.teacher_id is None
    assert "ambiguous pipe caption format" in ambiguous_teacher.review_reason


async def test_pipe_caption_with_two_teacher_fields_needs_review(
    session, topic_catalogue
):
    cat = topic_catalogue
    other_teacher, error = await add_teacher(
        session, cat.anatomy.id, "Dr Second Teacher"
    )
    assert error is None

    item = await _ingest(
        session,
        66,
        "Anatomy | Dr Mohammad Azam Sir | Lecture 1 | Dr Second Teacher",
    )
    assert item.status == "needs_review"
    assert item.teacher_id is None
    assert "ambiguous pipe caption format" in item.review_reason
    assert other_teacher.id != cat.teacher.id


def test_pipe_caption_backward_compatibility():
    titled = parse_caption(
        "Anatomy | Skull | Lecture 3: Cranial Nerves | Dr Mohammad Azam Sir",
        None,
    )
    assert titled.structured is True
    assert titled.caption_format == "pipe_legacy"
    assert titled.lecture_number == 3
    assert titled.lecture_title == "Cranial Nerves"
    assert titled.title == "Cranial Nerves"

    missing_teacher = parse_caption("Anatomy | Skull | Lecture 1", None)
    assert missing_teacher.structured is True
    assert missing_teacher.teacher_name is None
    assert missing_teacher.teacher_missing is True

    two_field = parse_caption(
        "Lecture 10: Pathophysiology | Dr Sharma",
        None,
    )
    assert two_field.structured is False
    assert two_field.lecture_number == 10
    assert two_field.teacher_name == "Dr Sharma"

    non_numeric_third_field = parse_caption(
        "Anatomy | Skull | Dr Azam",
        None,
    )
    assert non_numeric_third_field.structured is False
