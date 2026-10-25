from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Course, Lecture, Teacher, UploadItem
from bot.db.repo import (
    list_item_topics,
    list_subjects,
    replace_lecture_topics,
)

logger = logging.getLogger(__name__)

PUBLISHED = "published"

DEFAULT_SUBJECTS = [
    ("anatomy", "Anatomy", "🫀", 1),
    ("physiology", "Physiology", "⚡", 2),
    ("biochemistry", "Biochemistry", "🧪", 3),
    ("pathology", "Pathology", "🔬", 4),
    ("pharmacology", "Pharmacology", "💊", 5),
    ("microbiology", "Microbiology", "🦠", 6),
    ("forensic", "Forensic Medicine", "⚖️", 7),
    ("psm", "PSM / Community Medicine", "🏥", 8),
    ("medicine", "Medicine", "🩺", 9),
    ("surgery", "Surgery", "🔪", 10),
    ("obg", "OBG", "👶", 11),
    ("pediatrics", "Pediatrics", "🧸", 12),
    ("ophthalmology", "Ophthalmology", "👁️", 13),
    ("ent", "ENT", "👂", 14),
    ("orthopedics", "Orthopedics", "🦴", 15),
    ("dermatology", "Dermatology", "🧬", 16),
    ("psychiatry", "Psychiatry", "🧠", 17),
    ("radiology", "Radiology", "📡", 18),
    ("anesthesia", "Anesthesia", "💉", 19),
]


async def ensure_subjects(session):
    existing = await list_subjects(session)
    if existing:
        return
    from bot.db.models import Subject
    for slug, name, emoji, order in DEFAULT_SUBJECTS:
        session.add(Subject(slug=slug, name=name, emoji=emoji, order=order))
    await session.commit()


async def project_upload_to_lecture(
    session: AsyncSession, item: UploadItem
) -> tuple[Optional[Lecture], bool]:
    """Create or update the student lecture for a published upload.

    The caller owns the transaction. The unique source link makes this
    idempotent even if publishing or startup backfill runs repeatedly.
    """
    if item.publication_status != PUBLISHED and item.published_message_id is None:
        return None, False
    if item.course_id is None:
        return None, False

    course = await session.get(Course, item.course_id)
    if course is None:
        return None, False

    lecture = await session.scalar(
        select(Lecture).where(Lecture.source_upload_item_id == item.id)
    )
    created = lecture is None
    if lecture is None:
        lecture = Lecture(
            subject_id=course.subject_id,
            source_upload_item_id=item.id,
            title="",
        )
        session.add(lecture)

    raw_title = (item.title or item.file_name or "").strip()
    fallback_number = (
        item.lecture_number if item.lecture_number is not None else item.id
    )
    lecture.subject_id = course.subject_id
    lecture.title = (raw_title or f"Lecture {fallback_number}")[:256]

    # Read the teacher directly from the database. A publishing session may
    # hold an older UploadItem instance while an admin correction has already
    # been committed by another request.
    teacher_id = await session.scalar(
        select(UploadItem.teacher_id).where(UploadItem.id == item.id)
    )
    if teacher_id is not None:
        teacher = await session.get(Teacher, teacher_id)
        if teacher is None or teacher.subject_id != course.subject_id:
            logger.warning(
                "Ignoring invalid teacher #%s for upload item #%s (subject %s)",
                teacher_id,
                item.id,
                course.subject_id,
            )
            teacher_id = None
    lecture.teacher_id = teacher_id

    if item.lecture_number is not None:
        lecture.order = item.lecture_number
    elif created:
        with session.no_autoflush:
            max_order = await session.scalar(
                select(func.coalesce(func.max(Lecture.order), 0)).where(
                    Lecture.subject_id == course.subject_id
                )
            )
        lecture.order = (max_order or 0) + 1

    await session.flush()
    topics = await list_item_topics(session, item.id)
    await replace_lecture_topics(
        session, lecture.id, [topic.id for topic in topics]
    )
    return lecture, created


async def sync_published_uploads_to_catalog(session: AsyncSession) -> int:
    """Project all already-published uploads, including legacy publications."""
    result = await session.execute(
        select(UploadItem).where(
            or_(
                UploadItem.publication_status == PUBLISHED,
                UploadItem.published_message_id.is_not(None),
            )
        )
    )
    created = 0
    for item in result.scalars().all():
        try:
            lecture, was_created = await project_upload_to_lecture(session, item)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Failed to project upload item #%s", item.id)
            continue
        if lecture is not None and was_created:
            created += 1
    return created
