from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import Course, SystemSetting, UploadBatch, UploadItem
from bot.errors import ApprovedBatchError


# ---------- Settings ----------
async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    setting = await session.get(SystemSetting, key)
    return setting.value if setting else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    setting = await session.get(SystemSetting, key)
    if setting is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        setting.value = value
    await session.commit()


async def is_ingestion_paused(session: AsyncSession) -> bool:
    return (await get_setting(session, "ingestion_paused", "0")) == "1"


# ---------- Batches ----------
async def get_or_create_batch(session: AsyncSession, source_chat_id: int, batch_date: date) -> UploadBatch:
    existing = await session.execute(
        select(UploadBatch).where(
            UploadBatch.source_chat_id == source_chat_id,
            UploadBatch.batch_date == batch_date,
            UploadBatch.status == "open",
        )
    )
    batch = existing.scalar_one_or_none()
    if batch:
        return batch

    batch = UploadBatch(source_chat_id=source_chat_id, batch_date=batch_date, status="open")
    session.add(batch)
    try:
        await session.commit()
        await session.refresh(batch)
        return batch
    except IntegrityError:
        await session.rollback()
        existing = await session.execute(
            select(UploadBatch).where(
                UploadBatch.source_chat_id == source_chat_id,
                UploadBatch.batch_date == batch_date,
                UploadBatch.status == "open",
            )
        )
        return existing.scalar_one()


async def get_batch(session: AsyncSession, batch_id: int) -> Optional[UploadBatch]:
    return await session.get(UploadBatch, batch_id)


async def list_batches(session: AsyncSession, limit: int = 15) -> list[UploadBatch]:
    res = await session.execute(select(UploadBatch).order_by(UploadBatch.id.desc()).limit(limit))
    return list(res.scalars().all())


async def get_batch_items(session: AsyncSession, batch_id: int) -> list[UploadItem]:
    null_last = case((UploadItem.lecture_number.is_(None), 1), else_=0)
    res = await session.execute(
        select(UploadItem)
        .where(UploadItem.batch_id == batch_id)
        .order_by(null_last, UploadItem.lecture_number, UploadItem.id)
    )
    return list(res.scalars().all())


# ---------- Items ----------
async def get_item_by_source(
    session: AsyncSession, source_chat_id: int, source_message_id: int
) -> Optional[UploadItem]:
    res = await session.execute(
        select(UploadItem).where(
            UploadItem.source_chat_id == source_chat_id,
            UploadItem.source_message_id == source_message_id,
        )
    )
    return res.scalar_one_or_none()


async def get_item(session: AsyncSession, item_id: int) -> Optional[UploadItem]:
    return await session.get(UploadItem, item_id)


async def find_duplicate_file(
    session: AsyncSession,
    file_unique_id: str,
    exclude_item_id: int,
    source_chat_id: int,
    media_type: str,
) -> Optional[UploadItem]:
    res = await session.execute(
        select(UploadItem)
        .where(
            UploadItem.file_unique_id == file_unique_id,
            UploadItem.id != exclude_item_id,
            UploadItem.source_chat_id == source_chat_id,
            UploadItem.media_type == media_type,
        )
        .order_by(UploadItem.id.asc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def ensure_item_mutable(session: AsyncSession, item: UploadItem) -> None:
    batch = await session.get(UploadBatch, item.batch_id)
    if batch is not None and batch.status == "approved":
        raise ApprovedBatchError(
            f"Batch #{batch.id} is approved; item #{item.id} cannot be changed."
        )


async def skip_item(session: AsyncSession, item_id: int) -> bool:
    item = await session.get(UploadItem, item_id)
    if not item:
        return False
    await ensure_item_mutable(session, item)
    item.status = "skipped"
    item.review_reason = "skipped by admin"
    await session.commit()
    return True


async def approve_batch(session: AsyncSession, batch_id: int, admin_id: int) -> tuple[bool, str]:
    batch = await session.get(UploadBatch, batch_id)
    if not batch:
        return False, "Batch not found."
    if batch.status == "approved":
        return False, "Batch is already approved."

    items = await get_batch_items(session, batch_id)
    if not items:
        return False, "Batch has no items."

    blocked = [
        item for item in items
        if item.status in {"received", "needs_review", "failed", "paused"}
    ]
    if blocked:
        ids = ", ".join(f"#{item.id}" for item in blocked[:10])
        return False, f"Cannot approve: unresolved items {ids}. Use /skip, /keep, or /retry first."

    batch.status = "approved"
    batch.approved_by = admin_id
    batch.approved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await session.commit()
    return True, f"Batch #{batch.id} approved (no publishing in this milestone)."


# ---------- Courses ----------
async def list_courses(session: AsyncSession) -> list[Course]:
    res = await session.execute(
        select(Course)
        .options(selectinload(Course.subject))
        .order_by(Course.year, Course.name)
    )
    return list(res.scalars().all())


async def get_course(session: AsyncSession, course_id: int) -> Optional[Course]:
    res = await session.execute(
        select(Course).options(selectinload(Course.subject)).where(Course.id == course_id)
    )
    return res.scalar_one_or_none()


async def upsert_course(
    session: AsyncSession,
    subject_id: int,
    name: str,
    aliases: str = "",
    year: str = "",
    destination_chat_id: Optional[str] = None,
    topic_id: Optional[int] = None,
) -> tuple[Course, bool]:
    """Create or update a subject course.

    Courses are keyed by (subject, name). Only the values supplied by the
    caller are overwritten, so re-running the command never wipes an
    existing topic/destination or a legacy year value.
    """
    res = await session.execute(
        select(Course).where(Course.subject_id == subject_id, Course.name == name)
    )
    course = res.scalar_one_or_none()
    created = course is None
    if course is None:
        course = Course(subject_id=subject_id, name=name, year=year or "")
        session.add(course)
    else:
        if year:
            course.year = year
    if aliases:
        course.aliases = aliases
    if destination_chat_id is not None:
        course.destination_chat_id = destination_chat_id
    if topic_id is not None:
        course.topic_id = topic_id
    await session.commit()
    await session.refresh(course)
    return course, created


def _recompute_item_status(item: UploadItem) -> None:
    reasons: list[str] = []
    if item.duplicate_flag:
        reasons.append("suspected duplicate file")
    if not item.course_id:
        reasons.append("course/year not confidently identified")
    if item.lecture_number is None:
        reasons.append("lecture number missing")
    item.status = "needs_review" if reasons else "classified"
    item.review_reason = "; ".join(reasons) if reasons else None


async def set_item_course(
    session: AsyncSession, item_id: int, course_id: int
) -> tuple[Optional[UploadItem], Optional[str]]:
    item = await session.get(UploadItem, item_id)
    if not item:
        return None, "Item not found."
    await ensure_item_mutable(session, item)
    course = await session.get(Course, course_id)
    if not course:
        return None, "Course not found."
    item.course_id = course.id
    item.error = None
    _recompute_item_status(item)
    await session.commit()
    await session.refresh(item)
    return item, None


async def set_item_number(
    session: AsyncSession, item_id: int, number: int
) -> tuple[Optional[UploadItem], Optional[str]]:
    item = await session.get(UploadItem, item_id)
    if not item:
        return None, "Item not found."
    await ensure_item_mutable(session, item)
    if number < 1 or number > 999:
        return None, "Lecture number must be between 1 and 999."
    item.lecture_number = number
    item.error = None
    _recompute_item_status(item)
    await session.commit()
    await session.refresh(item)
    return item, None
