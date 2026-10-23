from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.ingestion_repo import (
    clear_teacher_if_subject_mismatch,
    ensure_item_mutable,
    find_duplicate_file,
    get_item_by_source,
    get_or_create_batch,
    get_setting,
    is_ingestion_paused,
    list_courses,
    set_setting,
)
from bot.db.models import Course, UploadItem


logger = logging.getLogger(__name__)


LECTURE_PATTERNS = (
    re.compile(r"\b(?:lecture|lec|class|video|part)\s*[-:#.]?\s*(\d{1,3})\b", re.IGNORECASE),
)

LEADING_TITLE_PREFIX = re.compile(
    r"^\s*(?:lecture|lec|class|video|part)\s*[-:#.]?\s*\d{1,3}\s*[-:.]?\s*",
    re.IGNORECASE,
)


@dataclass
class ParsedCaption:
    title: str
    lecture_number: Optional[int]
    number_ambiguous: bool
    numbers: list[int]


def _extract_numbers(text: str) -> list[int]:
    numbers: list[int] = []
    for pattern in LECTURE_PATTERNS:
        for match in pattern.finditer(text):
            number = int(match.group(1))
            if number not in numbers:
                numbers.append(number)

    leading = re.match(r"^\s*(\d{1,3})\s*[.)\-:]\s+", text)
    if leading:
        number = int(leading.group(1))
        if number not in numbers:
            numbers.append(number)
    return numbers


def parse_caption(caption: Optional[str], file_name: Optional[str]) -> ParsedCaption:
    text = (caption or "").strip()
    numbers = _extract_numbers(text)
    if not numbers and file_name:
        stem = os.path.splitext(os.path.basename(file_name))[0].strip()
        numbers = _extract_numbers(stem)

    number_ambiguous = len(numbers) > 1
    lecture_number = numbers[0] if len(numbers) == 1 else None

    title = ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            title = line
            break

    if not title and file_name:
        title = os.path.splitext(os.path.basename(file_name))[0].strip()

    if title:
        cleaned = LEADING_TITLE_PREFIX.sub("", title, count=1).strip()
        if cleaned:
            title = cleaned

    return ParsedCaption(
        title=(title or "Untitled")[:256],
        lecture_number=lecture_number,
        number_ambiguous=number_ambiguous,
        numbers=numbers,
    )


def _course_candidates(course: Course) -> list[str]:
    candidates = [course.name]
    if course.aliases:
        candidates.extend(part.strip() for part in course.aliases.split(","))
    return [c for c in candidates if len(c.strip()) >= 3]


def match_courses(courses: list[Course], text: str) -> list[Course]:
    folded = text.casefold()
    matched: list[Course] = []
    for course in courses:
        if any(candidate.strip().casefold() in folded for candidate in _course_candidates(course)):
            matched.append(course)
    return matched


async def classify_item(session: AsyncSession, item: UploadItem) -> UploadItem:
    parsed = parse_caption(item.raw_caption, item.file_name)
    item.title = parsed.title
    item.lecture_number = parsed.lecture_number

    text = " ".join(part for part in (item.raw_caption, item.file_name, item.title) if part)
    courses = await list_courses(session)
    matches = match_courses(courses, text)

    reasons: list[str] = []
    if item.duplicate_flag:
        reasons.append("suspected duplicate file")

    if len(matches) == 0:
        item.course_id = None
        reasons.append("course/year not confidently identified")
    elif len(matches) > 1:
        item.course_id = None
        reasons.append("multiple course matches")
    else:
        item.course_id = matches[0].id
        if await clear_teacher_if_subject_mismatch(
            session, item, matches[0].subject_id
        ):
            logger.info(
                "Cleared incompatible teacher from upload item #%s after "
                "course reclassification",
                item.id,
            )

    if parsed.number_ambiguous:
        reasons.append("ambiguous lecture number")
    elif parsed.lecture_number is None:
        reasons.append("lecture number missing")

    item.review_reason = "; ".join(reasons) if reasons else None
    item.status = "needs_review" if reasons else "classified"
    item.error = None
    return item


async def ingest_media(
    session: AsyncSession,
    *,
    source_chat_id: int,
    source_message_id: int,
    media_type: str,
    caption: Optional[str],
    file_name: Optional[str],
    file_unique_id: Optional[str],
    file_id: Optional[str] = None,
    forward_from_chat_id: Optional[int] = None,
    forward_from_message_id: Optional[int] = None,
) -> tuple[UploadItem, bool]:
    existing = await get_item_by_source(session, source_chat_id, source_message_id)
    if existing:
        return existing, False

    paused = await is_ingestion_paused(session)
    batch = await get_or_create_batch(session, source_chat_id, datetime.now(timezone.utc).date())
    parsed = parse_caption(caption, file_name)

    item = UploadItem(
        batch_id=batch.id,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        forward_from_chat_id=forward_from_chat_id,
        forward_from_message_id=forward_from_message_id,
        media_type=media_type,
        file_name=file_name,
        file_id=file_id,
        file_unique_id=file_unique_id,
        raw_caption=caption,
        title=parsed.title,
        lecture_number=parsed.lecture_number,
        status="paused" if paused else "received",
    )
    session.add(item)
    try:
        await session.commit()
        await session.refresh(item)
    except IntegrityError:
        await session.rollback()
        existing = await get_item_by_source(session, source_chat_id, source_message_id)
        if existing:
            return existing, False
        raise

    item_id = item.id
    if file_unique_id:
        duplicate = await find_duplicate_file(
            session, file_unique_id, item_id, source_chat_id, media_type
        )
        if duplicate:
            item.duplicate_flag = True
            item.duplicate_of_item_id = duplicate.id

    if paused:
        await session.commit()
        await session.refresh(item)
        return item, True

    try:
        await classify_item(session, item)
        await session.commit()
        await session.refresh(item)
    except Exception as exc:  # keep the stored item, record the failure
        logger.exception("Failed to classify upload item #%s", item_id)
        await session.rollback()
        failed = await session.get(UploadItem, item_id)
        if failed:
            failed.status = "failed"
            failed.error = str(exc)[:1000]
            await session.commit()
            await session.refresh(failed)
            return failed, True
        raise

    return item, True


async def retry_item(session: AsyncSession, item_id: int) -> Optional[UploadItem]:
    item = await session.get(UploadItem, item_id)
    if not item:
        return None
    await ensure_item_mutable(session, item)
    item.status = "received"
    item.error = None
    item.review_reason = None
    if item.file_unique_id:
        duplicate = await find_duplicate_file(
            session, item.file_unique_id, item.id, item.source_chat_id, item.media_type
        )
        if duplicate:
            item.duplicate_flag = True
            item.duplicate_of_item_id = duplicate.id
    await session.commit()
    await classify_item(session, item)
    await session.commit()
    await session.refresh(item)
    return item


async def keep_item(session: AsyncSession, item_id: int) -> Optional[UploadItem]:
    item = await session.get(UploadItem, item_id)
    if not item:
        return None
    await ensure_item_mutable(session, item)
    item.duplicate_flag = False
    item.duplicate_of_item_id = None
    item.status = "received"
    item.review_reason = None
    await session.commit()
    await classify_item(session, item)
    await session.commit()
    await session.refresh(item)
    return item


async def pause_ingestion(session: AsyncSession) -> None:
    await set_setting(session, "ingestion_paused", "1")


async def resume_ingestion(session: AsyncSession) -> int:
    await set_setting(session, "ingestion_paused", "0")
    from sqlalchemy import select

    res = await session.execute(select(UploadItem).where(UploadItem.status == "paused"))
    items = list(res.scalars().all())
    for item in items:
        await classify_item(session, item)
    await session.commit()
    return len(items)


async def ingestion_is_paused(session: AsyncSession) -> bool:
    return (await get_setting(session, "ingestion_paused", "0")) == "1"
