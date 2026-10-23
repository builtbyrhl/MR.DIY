from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from bot.db.ingestion_repo import approve_batch, set_item_teacher
from bot.db.models import Course, Lecture, Subject
from bot.db.repo import add_teacher
from bot.handlers import review
from bot.services.catalog import sync_published_uploads_to_catalog
from bot.services.ingestion import ingest_media, retry_item
from bot.services.publishing import publish_item


async def _subject_course(
    session,
    *,
    slug,
    subject_name,
    course_name,
    aliases,
    destination_chat_id=None,
    topic_id=None,
):
    subject = Subject(slug=slug, name=subject_name, emoji="📘", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    course = Course(
        year="",
        subject_id=subject.id,
        name=course_name,
        aliases=aliases,
        destination_chat_id=destination_chat_id,
        topic_id=topic_id,
    )
    session.add(course)
    await session.commit()
    await session.refresh(course)
    return subject, course


async def _teacher(session, subject_id, name):
    teacher, error = await add_teacher(session, subject_id, name)
    assert error is None
    return teacher


async def _ingest(session, caption, *, message_id=1, file_unique_id="teacher-file"):
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type="video",
        caption=caption,
        file_name=None,
        file_id="telegram-file-id",
        file_unique_id=file_unique_id,
    )
    return item


@pytest.mark.parametrize(
    "caption",
    [
        "Lecture 10: Pathophysiology — Dr Sharma",
        "Lecture 10: Pathophysiology – Dr Sharma",
        "Lecture 10: Pathophysiology - Dr Sharma",
        "Lecture 10: Pathophysiology | Dr Sharma",
        "Lecture 10: Pathophysiology — Teacher: Dr Sharma",
        "Lecture 10: Pathophysiology — by Dr Sharma",
    ],
)
async def test_auto_extracts_teacher_with_supported_separators(
    session, caption
):
    subject, course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )
    teacher = await _teacher(session, subject.id, "Dr. Sharma")

    item = await _ingest(session, caption)

    assert item.lecture_number == 10
    assert item.course_id == course.id
    assert item.teacher_id == teacher.id
    assert item.teacher_source == "auto"
    assert item.title == "Pathophysiology"
    assert item.status == "classified"
    assert item.teacher_review_reason is None


async def test_unknown_teacher_is_reported_and_not_created(session):
    subject, course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Dr Unknown"
    )

    assert item.course_id == course.id
    assert item.teacher_id is None
    assert item.teacher_source is None
    assert item.status == "needs_review"
    assert item.teacher_review_reason == "teacher not found: Dr Unknown"
    assert "teacher not found: Dr Unknown" in item.review_reason
    assert item.title == "Pathophysiology"

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/dryrun {item.batch_id}",
        answer=AsyncMock(),
    )
    await review.dryrun_cmd(message, session)
    output = "\n".join(call.args[0] for call in message.answer.await_args_list)
    assert "teacher not found: Dr Unknown" in output
    assert "teacher: not specified" in output


async def test_plain_name_after_em_dash_is_treated_as_teacher(session):
    subject, _course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )
    teacher = await _teacher(session, subject.id, "Sharma")

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Sharma", message_id=12
    )

    assert item.teacher_id == teacher.id
    assert item.title == "Pathophysiology"
    assert item.status == "classified"


async def test_plain_name_after_em_dash_that_does_not_match_is_reviewed(session):
    subject, _course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )
    await _teacher(session, subject.id, "Sharma")

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Rao", message_id=13
    )

    assert item.teacher_id is None
    assert item.status == "needs_review"
    assert item.teacher_review_reason == "teacher not found: Rao"
    assert item.title == "Pathophysiology"


async def test_retry_assigns_teacher_added_after_unknown_candidate(session):
    subject, _course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Dr Sharma", message_id=11
    )
    assert item.teacher_id is None
    assert item.status == "needs_review"

    teacher = await _teacher(session, subject.id, "Dr Sharma")
    item = await retry_item(session, item.id)

    assert item.teacher_id == teacher.id
    assert item.teacher_source == "auto"
    assert item.teacher_review_reason is None
    assert item.status == "classified"


async def test_teacher_name_is_matched_within_the_correct_subject(session):
    physiology, physiology_course = await _subject_course(
        session,
        slug="physiology",
        subject_name="Physiology",
        course_name="Physiology",
        aliases="physiology",
    )
    pathology, pathology_course = await _subject_course(
        session,
        slug="pathology",
        subject_name="Pathology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )
    other_teacher = await _teacher(session, physiology.id, "Dr Sharma")
    correct_teacher = await _teacher(session, pathology.id, "Dr Sharma")

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Dr Sharma", message_id=2
    )

    assert item.course_id == pathology_course.id
    assert item.course_id != physiology_course.id
    assert item.teacher_id == correct_teacher.id
    assert item.teacher_id != other_teacher.id
    assert item.status == "classified"


async def test_teacher_from_another_subject_is_not_assigned(session):
    physiology, physiology_course = await _subject_course(
        session,
        slug="physiology",
        subject_name="Physiology",
        course_name="Physiology",
        aliases="physiology",
    )
    pathology, _pathology_course = await _subject_course(
        session,
        slug="pathology",
        subject_name="Pathology",
        course_name="Pathology",
        aliases="pathology",
    )
    await _teacher(session, pathology.id, "Dr Sharma")

    item = await _ingest(
        session, "Lecture 10: Physiology — Dr Sharma", message_id=10
    )

    assert item.course_id == physiology_course.id
    assert physiology.id == physiology_course.subject_id
    assert item.teacher_id is None
    assert item.status == "needs_review"
    assert item.teacher_review_reason == "teacher not found: Dr Sharma"


async def test_unique_short_alias_can_match_word_prefix(session):
    _subject, course = await _subject_course(
        session,
        slug="anatomy",
        subject_name="Anatomy",
        course_name="Anatomy A",
        aliases="anat",
    )

    item = await _ingest(session, "Anatomy Lecture 1", message_id=9)

    assert item.course_id == course.id
    assert item.status == "classified"


async def test_pathophysiology_does_not_partially_match_physiology(session):
    _subject, physiology_course = await _subject_course(
        session,
        slug="physiology",
        subject_name="Physiology",
        course_name="Physiology",
        aliases="physiology,physio",
    )

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Dr Sharma", message_id=3
    )

    assert item.course_id is None
    assert item.course_id != physiology_course.id
    assert item.teacher_id is None
    assert item.status == "needs_review"
    assert "course/year not confidently identified" in item.review_reason
    assert (
        "teacher not resolved: subject not uniquely identified"
        in item.review_reason
    )


async def test_missing_subject_does_not_assign_teacher(session):
    subject, _course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )
    await _teacher(session, subject.id, "Dr Sharma")

    item = await _ingest(
        session, "Lecture 10: Unknown Subject — Dr Sharma", message_id=4
    )

    assert item.course_id is None
    assert item.teacher_id is None
    assert item.status == "needs_review"
    assert "course/year not confidently identified" in item.review_reason
    assert (
        "teacher not resolved: subject not uniquely identified"
        in item.review_reason
    )


async def test_overlapping_course_names_go_to_review(session):
    subject = Subject(slug="medicine", name="Medicine", emoji="🩺", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)
    session.add_all(
        [
            Course(
                year="",
                subject_id=subject.id,
                name="Medicine",
                aliases="medicine",
            ),
            Course(
                year="",
                subject_id=subject.id,
                name="Community Medicine",
                aliases="community medicine",
            ),
        ]
    )
    await session.commit()
    await _teacher(session, subject.id, "Dr Sharma")

    item = await _ingest(
        session,
        "Lecture 5: Community Medicine — Dr Sharma",
        message_id=5,
    )

    assert item.course_id is None
    assert item.teacher_id is None
    assert item.status == "needs_review"
    assert "multiple course matches" in item.review_reason
    assert (
        "teacher not resolved: subject not uniquely identified"
        in item.review_reason
    )


async def test_manual_teacher_survives_retry_and_catalogue_sync(session):
    subject, _course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )
    await _teacher(session, subject.id, "Dr Sharma")
    manual_teacher = await _teacher(session, subject.id, "Dr Rao")

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Dr Sharma", message_id=6
    )
    assert item.teacher_source == "auto"

    item, error = await set_item_teacher(session, item.id, manual_teacher.id)
    assert error is None
    assert item.teacher_source == "manual"

    item = await retry_item(session, item.id)
    assert item.teacher_id == manual_teacher.id
    assert item.teacher_source == "manual"

    item.publication_status = "published"
    item.published_message_id = 900
    await session.commit()
    await sync_published_uploads_to_catalog(session)

    lecture = await session.scalar(
        select(Lecture).where(Lecture.source_upload_item_id == item.id)
    )
    assert lecture.teacher_id == manual_teacher.id


async def test_manual_teacher_clear_survives_retry(session):
    subject, _course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
    )
    await _teacher(session, subject.id, "Dr Sharma")

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Dr Sharma", message_id=7
    )
    assert item.teacher_id is not None

    item, error = await set_item_teacher(session, item.id, None)
    assert error is None
    assert item.teacher_source == "manual"

    item = await retry_item(session, item.id)
    assert item.teacher_id is None
    assert item.teacher_source == "manual"


async def test_auto_teacher_reaches_student_catalogue_after_publish(session):
    subject, _course = await _subject_course(
        session,
        slug="pathophysiology",
        subject_name="Pathophysiology",
        course_name="Pathophysiology",
        aliases="pathophysiology",
        destination_chat_id="-100123",
        topic_id=8,
    )
    teacher = await _teacher(session, subject.id, "Dr Sharma")

    item = await _ingest(
        session, "Lecture 10: Pathophysiology — Dr Sharma", message_id=8
    )
    ok, message = await approve_batch(session, item.batch_id, 123)
    assert ok is True, message

    bot = AsyncMock()
    bot.send_video.return_value = SimpleNamespace(message_id=901)
    result = await publish_item(session, bot, item.id)
    assert result.status == "published"

    lecture = await session.scalar(
        select(Lecture).where(Lecture.source_upload_item_id == item.id)
    )
    assert lecture.teacher_id == teacher.id
    assert lecture.title == "Pathophysiology"
