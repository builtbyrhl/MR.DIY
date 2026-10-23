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
from bot.db.repo import find_matching_teachers


logger = logging.getLogger(__name__)


LECTURE_PATTERNS = (
    re.compile(r"\b(?:lecture|lec|class|video|part)\s*[-:#.]?\s*(\d{1,3})\b", re.IGNORECASE),
)

LEADING_TITLE_PREFIX = re.compile(
    r"^\s*(?:lecture|lec|class|video|part)\s*[-:#.]?\s*\d{1,3}\s*[-:.]?\s*",
    re.IGNORECASE,
)

# Teacher extraction is intentionally conservative. A trailing segment is only
# removed from the title when it is explicitly marked as a teacher or when it
# matches a teacher in the uniquely identified subject.
TEACHER_SEPARATOR = re.compile(r"(?:\s*[—–]\s*|\s+\|\s+|\s+--\s+|\s+-\s+)")
TEACHER_LABEL_PREFIX = re.compile(
    r"^(?:(?:teacher|faculty)\s*[:\-]\s*|by\s+)", re.IGNORECASE
)
TEACHER_HONORIFIC = re.compile(
    r"^(?:dr|prof|professor|mr|mrs|ms|miss|sir|madam|"
    r"asst\.?\s+prof(?:essor)?|assoc\.?\s+prof(?:essor)?)\b",
    re.IGNORECASE,
)
COURSE_TOKEN = re.compile(r"\w+", re.UNICODE)


@dataclass
class ParsedCaption:
    title: str
    lecture_number: Optional[int]
    number_ambiguous: bool
    numbers: list[int]
    teacher_name: Optional[str] = None
    title_without_teacher: Optional[str] = None
    teacher_explicit: bool = False


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


def _split_teacher(title: str) -> tuple[Optional[str], Optional[str], bool]:
    """Split a trailing teacher segment from a title.

    Returns ``(title_without_teacher, teacher_name, explicit)``. ``explicit`` is
    true when the candidate uses a documented label or an honorific, allowing
    the caller to report an unknown teacher without treating every hyphenated
    subtitle (for example "Upper Limb") as a failed teacher match.
    """
    separators = list(TEACHER_SEPARATOR.finditer(title))
    if not separators:
        return None, None, False

    separator = separators[-1]
    base = title[: separator.start()].strip(" \t-—–:|,;")
    candidate = title[separator.end() :].strip(" \t-—–:|,;")
    if not base or not candidate or len(candidate) > 128:
        return None, None, False

    separator_text = separator.group(0)
    explicit = any(marker in separator_text for marker in "—–|")
    label = TEACHER_LABEL_PREFIX.match(candidate)
    if label:
        candidate = candidate[label.end() :].strip(" \t-—–:|,;")
        explicit = True
    if TEACHER_HONORIFIC.match(candidate):
        explicit = True
    if len(candidate) < 2:
        return None, None, False
    return base, candidate, explicit


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

    title_without_teacher = None
    teacher_name = None
    teacher_explicit = False
    if title:
        title_without_teacher, teacher_name, teacher_explicit = _split_teacher(title)

    return ParsedCaption(
        title=(title or "Untitled")[:256],
        lecture_number=lecture_number,
        number_ambiguous=number_ambiguous,
        numbers=numbers,
        teacher_name=teacher_name,
        title_without_teacher=title_without_teacher,
        teacher_explicit=teacher_explicit,
    )


def _course_candidates(course: Course) -> list[str]:
    candidates = [course.name]
    if course.aliases:
        candidates.extend(part.strip() for part in course.aliases.split(","))
    return [c for c in candidates if len(c.strip()) >= 3]


def _match_tokens(text: str) -> list[str]:
    return COURSE_TOKEN.findall(text.casefold())


def _contains_token_phrase(text_tokens: list[str], candidate: str) -> bool:
    candidate_tokens = _match_tokens(candidate)
    if not candidate_tokens:
        return False
    width = len(candidate_tokens)
    return any(
        text_tokens[index : index + width] == candidate_tokens
        for index in range(len(text_tokens) - width + 1)
    )


def _contains_token_prefix(text_tokens: list[str], candidate: str) -> bool:
    candidate_tokens = _match_tokens(candidate)
    if len(candidate_tokens) != 1:
        return False
    return any(token.startswith(candidate_tokens[0]) for token in text_tokens)


def match_courses(courses: list[Course], text: str) -> list[Course]:
    """Match exact course words first, with unique aliases as a safe fallback.

    Whole-token phrase matches prevent ``Physiology`` from matching inside
    ``Pathophysiology``. A single-token alias such as ``anat`` may match the
    start of ``Anatomy`` only when no exact course-name/alias match exists.
    Multiple matches at either level remain ambiguous and are reviewed.
    """
    text_tokens = _match_tokens(text)
    exact: list[Course] = []
    prefix: list[Course] = []
    for course in courses:
        candidates = _course_candidates(course)
        if any(
            _contains_token_phrase(text_tokens, candidate)
            for candidate in candidates
        ):
            exact.append(course)
        elif any(
            _contains_token_prefix(text_tokens, candidate)
            for candidate in candidates
        ):
            prefix.append(course)
    return exact or prefix


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

    course = None
    if len(matches) == 0:
        item.course_id = None
        reasons.append("course/year not confidently identified")
    elif len(matches) > 1:
        item.course_id = None
        reasons.append("multiple course matches")
    else:
        course = matches[0]
        item.course_id = course.id
        if await clear_teacher_if_subject_mismatch(
            session, item, course.subject_id
        ):
            logger.info(
                "Cleared incompatible teacher from upload item #%s after "
                "course reclassification",
                item.id,
            )

    teacher_reason = None
    auto_teacher_id = None
    if course is None:
        if item.teacher_source == "auto":
            item.teacher_id = None
            item.teacher_source = None
        if (
            parsed.teacher_name
            and parsed.teacher_explicit
            and item.teacher_id is None
        ):
            teacher_reason = "teacher not resolved: subject not uniquely identified"
    else:
        manual_teacher = item.teacher_source == "manual" or (
            item.teacher_id is not None and item.teacher_source is None
        )
        if not manual_teacher:
            if parsed.teacher_name:
                teachers = await find_matching_teachers(
                    session, course.subject_id, parsed.teacher_name
                )
                if len(teachers) == 1:
                    item.teacher_id = teachers[0].id
                    item.teacher_source = "auto"
                    auto_teacher_id = teachers[0].id
                else:
                    if item.teacher_source == "auto":
                        item.teacher_id = None
                        item.teacher_source = None
                    if len(teachers) > 1:
                        teacher_reason = (
                            f"ambiguous teacher: {parsed.teacher_name}"
                        )
                    elif parsed.teacher_explicit:
                        teacher_reason = (
                            f"teacher not found: {parsed.teacher_name}"
                        )
            elif item.teacher_source == "auto":
                item.teacher_id = None
                item.teacher_source = None

    item.teacher_review_reason = teacher_reason
    if teacher_reason:
        reasons.append(teacher_reason)

    if (
        parsed.title_without_teacher
        and (parsed.teacher_explicit or auto_teacher_id is not None)
    ):
        item.title = parsed.title_without_teacher[:256]

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
    item.teacher_review_reason = None
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
    item.teacher_review_reason = None
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
