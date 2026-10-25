from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Course,
    Lecture,
    Subject,
    Topic,
    UploadItem,
    lecture_topics,
)
from bot.student_ui import resolve_lecture_title


async def decorate_lecture_titles(
    session: AsyncSession, lectures: list[Lecture]
) -> None:
    """Attach a display-only title using topics and the source upload caption."""
    items = [lecture for lecture in lectures if lecture is not None]
    if not items:
        return

    upload_ids = {
        lecture.source_upload_item_id
        for lecture in items
        if lecture.source_upload_item_id is not None
    }
    uploads: dict[int, UploadItem] = {}
    if upload_ids:
        result = await session.execute(
            select(UploadItem).where(UploadItem.id.in_(upload_ids))
        )
        uploads = {upload.id: upload for upload in result.scalars().all()}

    subject_ids = {lecture.subject_id for lecture in items}
    result = await session.execute(
        select(Subject).where(Subject.id.in_(subject_ids))
    )
    subjects = {subject.id: subject for subject in result.scalars().all()}

    course_ids = {
        upload.course_id
        for upload in uploads.values()
        if upload.course_id is not None
    }
    courses: dict[int, Course] = {}
    if course_ids:
        result = await session.execute(
            select(Course).where(Course.id.in_(course_ids))
        )
        courses = {course.id: course for course in result.scalars().all()}

    lecture_ids = {lecture.id for lecture in items}
    topic_names: dict[int, list[str]] = {}
    if lecture_ids:
        result = await session.execute(
            select(lecture_topics.c.lecture_id, Topic.name)
            .join(Topic, Topic.id == lecture_topics.c.topic_id)
            .where(lecture_topics.c.lecture_id.in_(lecture_ids))
            .order_by(Topic.name, Topic.id)
        )
        for lecture_id, name in result.all():
            topic_names.setdefault(lecture_id, []).append(name)

    for lecture in items:
        upload = uploads.get(lecture.source_upload_item_id or 0)
        subject = subjects.get(lecture.subject_id)
        course = courses.get(upload.course_id) if upload else None
        lecture.display_title = resolve_lecture_title(
            lecture.title,
            subject_name=subject.name if subject else None,
            course_name=course.name if course else None,
            topic_names=topic_names.get(lecture.id, ()),
            upload_title=upload.title if upload else None,
            raw_caption=upload.raw_caption if upload else None,
            original_caption=upload.original_caption if upload else None,
            file_name=upload.file_name if upload else None,
        )
