from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from bot.db.ingestion_repo import (
    approve_batch,
    set_item_course,
    set_item_topics,
    skip_item,
)
from bot.db.models import Course, Subject
from bot.db.repo import (
    add_teacher,
    add_teacher_alias,
    add_topic,
    list_item_topics,
)
from bot.handlers import review
from bot.services.ingestion import ingest_media


@pytest_asyncio.fixture
async def teacher_catalogue(session):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="🫀", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    course = Course(year="", subject_id=subject.id, name="Anatomy", aliases="anatomy")
    session.add(course)
    await session.commit()
    await session.refresh(course)

    old_teacher, error = await add_teacher(session, subject.id, "Dr Old")
    assert error is None
    new_teacher, error = await add_teacher(session, subject.id, "Dr New")
    assert error is None
    topic, error = await add_topic(session, subject.id, "System")
    assert error is None

    return SimpleNamespace(
        subject=subject,
        course=course,
        old_teacher=old_teacher,
        new_teacher=new_teacher,
        topic=topic,
    )


async def _ingest(session, message_id, *, caption=None, file_name=None):
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type="video",
        caption=caption,
        file_name=file_name,
        file_id=f"file-{message_id}",
        file_unique_id=f"unique-{message_id}",
    )
    return item


async def _classified_items(session, cat, count=3):
    items = []
    for index in range(1, count + 1):
        item = await _ingest(
            session,
            index,
            caption=(
                f"Anatomy | Dr Old | System | Title {index} | L{index}"
            ),
        )
        assert item.status == "classified"
        items.append(item)
    return items


def _plan_message(raw, *, apply=False):
    suffix = " apply" if apply else ""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setteacherplan {raw}{suffix}",
        answer=AsyncMock(),
    )


def _generate_message(batch_id):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/generatecaptions {batch_id}",
        answer=AsyncMock(),
    )


def _answer_text(message):
    return "\n".join(call.args[0] for call in message.answer.await_args_list)


async def test_setteacherplan_previews_batch_teacher_change_without_changes(
    session, teacher_catalogue
):
    cat = teacher_catalogue
    items = await _classified_items(session, cat)
    batch_id = items[0].batch_id

    message = _plan_message(f"batch:{batch_id}|Dr New")
    await review.setteacherplan_cmd(message, session)

    text = _answer_text(message)
    assert "Teacher assignment preview" in text
    assert "No database changes made" in text
    assert (
        f"current: #{cat.old_teacher.id} Dr Old -> proposed: "
        f"#{cat.new_teacher.id} Dr New"
    ) in text

    for item in items:
        await session.refresh(item)
        assert item.teacher_id == cat.old_teacher.id


async def test_setteacherplan_applies_batch_teacher_by_id_and_preserves_metadata(
    session, teacher_catalogue
):
    cat = teacher_catalogue
    items = await _classified_items(session, cat)
    batch_id = items[0].batch_id
    original_topics = [
        [topic.id for topic in await list_item_topics(session, item.id)]
        for item in items
    ]

    message = _plan_message(f"batch:{batch_id}|{cat.new_teacher.id}", apply=True)
    await review.setteacherplan_cmd(message, session)

    for index, item in enumerate(items, 1):
        await session.refresh(item)
        assert item.teacher_id == cat.new_teacher.id
        assert item.teacher_source == "manual"
        assert item.course_id == cat.course.id
        assert item.lecture_number == index
        assert item.status == "classified"
        assert [
            topic.id for topic in await list_item_topics(session, item.id)
        ] == original_topics[index - 1]

    assert "3 updated" in _answer_text(message)


async def test_setteacherplan_applies_selected_items_only(session, teacher_catalogue):
    cat = teacher_catalogue
    items = await _classified_items(session, cat)

    message = _plan_message(
        f"items:{items[0].id},{items[1].id}|Dr New", apply=True
    )
    await review.setteacherplan_cmd(message, session)

    await session.refresh(items[0])
    await session.refresh(items[1])
    await session.refresh(items[2])
    assert items[0].teacher_id == cat.new_teacher.id
    assert items[1].teacher_id == cat.new_teacher.id
    assert items[2].teacher_id == cat.old_teacher.id
    assert "2 updated" in _answer_text(message)
    assert "1 skipped" not in _answer_text(message)


async def test_setteacherplan_resolves_teacher_aliases(session, teacher_catalogue):
    cat = teacher_catalogue
    items = await _classified_items(session, cat, count=1)
    alias, error = await add_teacher_alias(session, cat.new_teacher.id, "New")
    assert error is None
    assert alias is not None

    message = _plan_message(f"batch:{items[0].batch_id}|New", apply=True)
    await review.setteacherplan_cmd(message, session)

    await session.refresh(items[0])
    assert items[0].teacher_id == cat.new_teacher.id


async def test_setteacherplan_flags_missing_teacher_for_review(
    session, teacher_catalogue
):
    cat = teacher_catalogue
    items = await _classified_items(session, cat, count=2)
    batch_id = items[0].batch_id

    preview = _plan_message(f"batch:{batch_id}|Dr Missing")
    await review.setteacherplan_cmd(preview, session)
    assert "teacher not found: Dr Missing" in _answer_text(preview)

    apply = _plan_message(f"batch:{batch_id}|Dr Missing", apply=True)
    await review.setteacherplan_cmd(apply, session)

    for index, item in enumerate(items, 1):
        await session.refresh(item)
        assert item.teacher_id == cat.old_teacher.id
        assert item.status == "needs_review"
        assert item.teacher_review_reason == "teacher not found: Dr Missing"
        assert item.lecture_number == index
        assert [
            topic.id for topic in await list_item_topics(session, item.id)
        ] == [cat.topic.id]

    assert "2 flagged" in _answer_text(apply)


async def test_setteacherplan_flags_ambiguous_teacher_for_review(
    session, teacher_catalogue, monkeypatch
):
    cat = teacher_catalogue
    items = await _classified_items(session, cat, count=1)

    async def fake_find_matching_teachers(session, subject_id, name):
        assert name == "Ambiguous"
        return [cat.old_teacher, cat.new_teacher]

    monkeypatch.setattr(review, "find_matching_teachers", fake_find_matching_teachers)

    preview = _plan_message(f"batch:{items[0].batch_id}|Ambiguous")
    await review.setteacherplan_cmd(preview, session)
    assert "ambiguous teacher: Ambiguous" in _answer_text(preview)

    apply = _plan_message(f"batch:{items[0].batch_id}|Ambiguous", apply=True)
    await review.setteacherplan_cmd(apply, session)

    await session.refresh(items[0])
    assert items[0].teacher_id == cat.old_teacher.id
    assert items[0].status == "needs_review"
    assert "ambiguous teacher: Ambiguous" in (items[0].teacher_review_reason or "")
    assert "1 flagged" in _answer_text(apply)


async def test_setteacherplan_does_not_change_approved_batch(
    session, teacher_catalogue
):
    cat = teacher_catalogue
    items = await _classified_items(session, cat, count=1)
    ok, text = await approve_batch(session, items[0].batch_id, 123)
    assert ok is True, text

    message = _plan_message(f"batch:{items[0].batch_id}|Dr New", apply=True)
    await review.setteacherplan_cmd(message, session)

    await session.refresh(items[0])
    assert items[0].teacher_id == cat.old_teacher.id
    assert "approved" in _answer_text(message)


async def test_setteacherplan_does_not_change_published_or_skipped_items(
    session, teacher_catalogue
):
    cat = teacher_catalogue
    published = await _ingest(
        session,
        10,
        caption="Anatomy | Dr Old | System | Published | L1",
    )
    published.publication_status = "published"
    await session.commit()

    skipped = await _ingest(
        session,
        11,
        caption="Anatomy | Dr Old | System | Skipped | L2",
    )
    assert await skip_item(session, skipped.id) is True

    message = _plan_message(
        f"items:{published.id},{skipped.id}|Dr New", apply=True
    )
    await review.setteacherplan_cmd(message, session)

    await session.refresh(published)
    await session.refresh(skipped)
    assert published.teacher_id == cat.old_teacher.id
    assert skipped.teacher_id == cat.old_teacher.id
    assert "2 skipped" in _answer_text(message)


async def test_setteacherplan_skips_items_without_course(session, teacher_catalogue):
    cat = teacher_catalogue
    item = await _ingest(session, 20, caption=None, file_name=None)

    message = _plan_message(f"items:{item.id}|Dr New", apply=True)
    await review.setteacherplan_cmd(message, session)

    await session.refresh(item)
    assert item.teacher_id is None
    assert "course/subject missing" in _answer_text(message)


async def test_generatecaptions_uses_teacher_assigned_by_setteacherplan(
    session, teacher_catalogue
):
    cat = teacher_catalogue
    item = await _ingest(
        session, 30, caption=None, file_name="01 Title.mp4"
    )
    item, error = await set_item_course(session, item.id, cat.course.id)
    assert error is None
    item, error = await set_item_topics(session, item.id, [cat.topic.id])
    assert error is None

    assign = _plan_message(f"items:{item.id}|Dr New", apply=True)
    await review.setteacherplan_cmd(assign, session)

    generate = _generate_message(item.batch_id)
    await review.generatecaptions_cmd(generate, session)

    assert (
        "Anatomy | Dr New | System | Title | L1" in _answer_text(generate)
    )
