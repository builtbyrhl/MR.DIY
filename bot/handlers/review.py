from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.ingestion_repo import (
    approve_batch,
    assign_item,
    create_named_batch,
    finish_collecting_batch,
    flag_item_teacher_review,
    get_active_collecting_batch,
    get_batch,
    get_batch_by_id,
    get_batch_items,
    get_course,
    get_open_batch_for_date,
    list_batches,
    list_courses,
    move_items_to_batch,
    set_item_course,
    set_item_number,
    set_item_teacher,
    set_item_topics,
    set_items_teacher_bulk,
    skip_item,
    start_collecting_batch,
    upsert_course,
)
from bot.config import get_settings
from bot.db.models import Teacher, UploadItem
from bot.db.repo import (
    add_teacher,
    add_teacher_alias,
    add_topic,
    find_matching_teachers,
    get_subject,
    list_item_topics,
    list_teacher_aliases,
    list_teachers,
    list_topics,
    remove_teacher_alias,
    rename_teacher,
    set_lecture_teacher,
    set_lecture_title,
    set_lecture_topics,
)
from bot.errors import ApprovedBatchError
from bot.services.auth import require_admin
from bot.services.ingestion import (
    CAPTION_EDIT_BLOCKED_STATUSES,
    keep_item,
    parse_caption,
    parse_filename,
    pause_ingestion,
    preferred_caption_error,
    reclassify_item_caption,
    resume_ingestion,
    retry_item,
    set_item_caption,
    update_ingested_caption,
)
from bot.services.publishing import (
    publish_batch,
    reconcile_publish_state,
    unlock_publish_state,
)

router = Router()
logger = logging.getLogger(__name__)

MAX_BULK_ITEMS = 200
_MAX_DB_ID = 2_147_483_647


def _authorized(message: Message) -> bool:
    try:
        require_admin(message.from_user.id)
        return True
    except PermissionError:
        return False



async def _reply_lines(message: Message, lines: list[str]) -> None:
    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > 3800:
            try:
                await message.answer(chunk)
            except TelegramBadRequest:
                await message.answer(chunk, parse_mode=None)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        try:
            await message.answer(chunk)
        except TelegramBadRequest:
            await message.answer(chunk, parse_mode=None)


async def _reply_plain_lines(message: Message, lines: list[str]) -> None:
    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > 3800:
            await message.answer(chunk, parse_mode=None)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await message.answer(chunk, parse_mode=None)


@dataclass
class _CaptionRefreshPlan:
    item: UploadItem
    action: str
    reason: str
    source_caption: Optional[str] = None
    source_error: Optional[str] = None
    current_number: Optional[int] = None
    source_number: Optional[int] = None
    proposed_number: Optional[int] = None
    replacement_caption: Optional[str] = None


def _source_replacement_error(
    source_parsed,
    known_number: Optional[int],
) -> Optional[str]:
    if known_number is None or source_parsed.lecture_number is None:
        return None
    if source_parsed.lecture_number == known_number:
        return None
    return (
        f"recovered source number L{source_parsed.lecture_number} "
        f"conflicts with existing L{known_number}"
    )


def _caption_with_number(caption: str, parsed, number: int) -> str:
    if parsed.lecture_number is not None:
        return caption.strip()
    if parsed.caption_format == "pipe_preferred":
        separator = " | "
    elif parsed.caption_format == "slash_preferred":
        separator = " / "
    else:
        return caption.strip()

    lines = caption.strip().splitlines()
    for index, line in enumerate(lines):
        if line.strip():
            lines[index] = f"{line.strip()}{separator}L{number}"
            break
    return "\n".join(lines)


def _caption_refresh_plan(
    item: UploadItem,
    source_caption: Optional[str],
    source_error: Optional[str],
) -> _CaptionRefreshPlan:
    current_parsed = parse_caption(item.raw_caption, item.file_name)
    source_parsed = (
        parse_caption(source_caption, item.file_name)
        if source_caption is not None
        else None
    )
    file_number, _ = parse_filename(item.file_name)
    current_number = current_parsed.lecture_number
    source_number = (
        source_parsed.lecture_number
        if source_parsed is not None
        and source_parsed.structured
        and not source_parsed.number_ambiguous
        else None
    )
    known_number = (
        current_number if current_number is not None else item.lecture_number
    )
    proposed_number = known_number
    if proposed_number is None:
        proposed_number = (
            source_number if source_number is not None else file_number
        )

    if item.status == "skipped":
        return _CaptionRefreshPlan(
            item,
            "skip",
            "item is skipped",
            source_caption,
            source_error,
            current_number=current_number,
            source_number=source_number,
            proposed_number=proposed_number,
        )
    if (
        item.status in CAPTION_EDIT_BLOCKED_STATUSES
        or item.publication_status in {"publishing", "published"}
    ):
        return _CaptionRefreshPlan(
            item,
            "skip",
            "item is publishing, published, or otherwise locked",
            source_caption,
            source_error,
            current_number=current_number,
            source_number=source_number,
            proposed_number=proposed_number,
        )

    current_error = preferred_caption_error(item.raw_caption, item.file_name)
    source_valid = (
        source_caption is not None
        and preferred_caption_error(source_caption, item.file_name) is None
    )

    if current_error is None:
        if (
            current_number is None
            and source_valid
            and source_number is not None
            and _source_replacement_error(source_parsed, known_number) is None
        ):
            return _CaptionRefreshPlan(
                item,
                "update",
                "populate lecture number from recovered source",
                source_caption,
                source_error,
                current_number=current_number,
                source_number=source_number,
                proposed_number=proposed_number,
                replacement_caption=source_caption,
            )
        if item.status == "classified" and (
            current_number is None
            or current_number == item.lecture_number
        ):
            return _CaptionRefreshPlan(
                item,
                "preserve",
                "current preferred caption is already valid",
                source_caption,
                source_error,
                current_number=current_number,
                source_number=source_number,
                proposed_number=proposed_number,
            )
        return _CaptionRefreshPlan(
            item,
            "reclassify",
            "keep current preferred caption and reclassify",
            source_caption,
            source_error,
            current_number=current_number,
            source_number=source_number,
            proposed_number=proposed_number,
        )

    if source_valid:
        replacement_error = _source_replacement_error(
            source_parsed, known_number
        )
        if replacement_error is not None:
            return _CaptionRefreshPlan(
                item,
                "skip",
                replacement_error,
                source_caption,
                source_error,
                current_number=current_number,
                source_number=source_number,
                proposed_number=proposed_number,
            )
        replacement_caption = source_caption
        if source_number is None and known_number is not None:
            replacement_caption = _caption_with_number(
                source_caption, source_parsed, known_number
            )
        return _CaptionRefreshPlan(
            item,
            "update",
            "replace non-preferred caption with recovered source",
            source_caption,
            source_error,
            current_number=current_number,
            source_number=source_number,
            proposed_number=proposed_number,
            replacement_caption=replacement_caption,
        )

    if source_caption is None:
        reason = "source unavailable and current caption is not preferred"
        if source_error:
            reason += f": {source_error}"
        return _CaptionRefreshPlan(
            item,
            "skip",
            reason,
            source_caption,
            source_error,
            current_number=current_number,
            source_number=source_number,
            proposed_number=proposed_number,
        )
    return _CaptionRefreshPlan(
        item,
        "skip",
        "recovered source does not use the preferred layout",
        source_caption,
        source_error,
        current_number=current_number,
        source_number=source_number,
        proposed_number=proposed_number,
    )


def _telegram_error_text(exc: Exception) -> str:
    detail = " ".join(str(exc).split())
    return f"{detail[:220]}" if detail else exc.__class__.__name__


async def _fetch_source_caption(
    bot: Bot, admin_chat_id: int, item: UploadItem
) -> tuple[Optional[str], Optional[str]]:
    forwarded = None
    try:
        forwarded = await bot.forward_message(
            chat_id=admin_chat_id,
            from_chat_id=item.source_chat_id,
            message_id=item.source_message_id,
            disable_notification=True,
        )
    except TelegramForbiddenError as exc:
        return None, "source chat is not accessible to the bot: " + (
            _telegram_error_text(exc)
        )
    except TelegramBadRequest as exc:
        return None, "source message is unavailable or protected: " + (
            _telegram_error_text(exc)
        )
    except TelegramAPIError as exc:
        return None, "Telegram could not retrieve the source message: " + (
            _telegram_error_text(exc)
        )
    except Exception as exc:
        logger.exception(
            "Failed to forward source message %s/%s for item #%s",
            item.source_chat_id,
            item.source_message_id,
            item.id,
        )
        return None, "unexpected error retrieving source message: " + (
            _telegram_error_text(exc)
        )

    forwarded_id = getattr(forwarded, "message_id", None)
    if forwarded_id is not None:
        try:
            await bot.delete_message(
                chat_id=admin_chat_id, message_id=forwarded_id
            )
        except Exception:
            logger.warning(
                "Could not delete temporary forwarded message %s in chat %s",
                forwarded_id,
                admin_chat_id,
                exc_info=True,
            )
    else:
        logger.warning(
            "Forwarded source message for item #%s has no message ID",
            item.id,
        )

    caption = (getattr(forwarded, "caption", None) or "").strip()
    if not caption:
        return None, "source message has no caption"
    return caption, None


def _number_label(number: Optional[int]) -> str:
    return f"L{number}" if number is not None else "L?"


def _refresh_plan_lines(plan: _CaptionRefreshPlan) -> list[str]:
    lines = [
        f"#{plan.item.id} [{plan.item.status}] action: {plan.action} - {plan.reason}",
        f"  current: {plan.item.raw_caption or '(none)'}",
    ]
    if plan.source_caption is not None:
        lines.append(f"  source: {plan.source_caption}")
    else:
        lines.append(
            f"  source: unavailable - {plan.source_error or 'not available'}"
        )
    lines.append(
        "  numbers: "
        f"current={_number_label(plan.current_number)}, "
        f"source={_number_label(plan.source_number)}, "
        f"proposed={_number_label(plan.proposed_number)}"
    )
    return lines


def _source_unavailable_note(plan: _CaptionRefreshPlan) -> str:
    if not plan.source_error:
        return ""
    return f" (source unavailable: {plan.source_error})"


@dataclass
class _GeneratedCaptionPlan:
    item: UploadItem
    action: str
    reason: str
    generated_caption: Optional[str] = None
    file_number: Optional[int] = None
    file_title: Optional[str] = None


def _sanitize_caption_field(value: Optional[str]) -> str:
    return " ".join((value or "").split()).replace("|", "/")


async def _generated_caption_plan(
    session, item: UploadItem
) -> _GeneratedCaptionPlan:
    current_error = preferred_caption_error(item.raw_caption, item.file_name)
    file_number, file_title = parse_filename(item.file_name)

    if item.status == "skipped":
        return _GeneratedCaptionPlan(
            item, "skip", "item is skipped", file_number=file_number, file_title=file_title
        )
    if (
        item.status in CAPTION_EDIT_BLOCKED_STATUSES
        or item.publication_status in {"publishing", "published"}
    ):
        return _GeneratedCaptionPlan(
            item,
            "skip",
            "item is publishing, published, or otherwise locked",
            file_number=file_number,
            file_title=file_title,
        )
    if current_error is None:
        return _GeneratedCaptionPlan(
            item,
            "preserve",
            "current caption is already valid",
            file_number=file_number,
            file_title=file_title,
        )
    if file_number is None or not file_title:
        return _GeneratedCaptionPlan(
            item,
            "skip",
            "filename has no clear lecture number and title",
            file_number=file_number,
            file_title=file_title,
        )

    course = None
    if item.course_id is not None:
        course = await get_course(session, item.course_id)
    if course is None or course.subject is None:
        return _GeneratedCaptionPlan(
            item,
            "skip",
            "course/subject missing; assign a course before generating captions",
            file_number=file_number,
            file_title=file_title,
        )

    teacher = None
    if item.teacher_id is not None:
        teacher = await session.get(Teacher, item.teacher_id)
    if teacher is None or teacher.subject_id != course.subject_id:
        return _GeneratedCaptionPlan(
            item,
            "skip",
            "teacher missing; assign a teacher before generating captions",
            file_number=file_number,
            file_title=file_title,
        )

    topics = await list_item_topics(session, item.id)
    if not topics:
        return _GeneratedCaptionPlan(
            item,
            "skip",
            "topic missing; assign a topic before generating captions",
            file_number=file_number,
            file_title=file_title,
        )
    if len(topics) > 1:
        return _GeneratedCaptionPlan(
            item,
            "skip",
            "multiple topics assigned; keep exactly one topic before generating captions",
            file_number=file_number,
            file_title=file_title,
        )

    subject_name = _sanitize_caption_field(course.subject.name)
    teacher_name = _sanitize_caption_field(teacher.name)
    topic_name = _sanitize_caption_field(topics[0].name)
    video_title = _sanitize_caption_field(file_title)
    if not subject_name or not teacher_name or not topic_name or not video_title:
        return _GeneratedCaptionPlan(
            item,
            "skip",
            "generated caption has an empty field",
            file_number=file_number,
            file_title=file_title,
        )

    generated_caption = (
        f"{subject_name} | {teacher_name} | {topic_name} | "
        f"{video_title} | L{file_number}"
    )
    generated_error = preferred_caption_error(generated_caption, None)
    if generated_error is not None:
        return _GeneratedCaptionPlan(
            item,
            "skip",
            f"generated caption is invalid: {generated_error}",
            generated_caption=generated_caption,
            file_number=file_number,
            file_title=file_title,
        )

    return _GeneratedCaptionPlan(
        item,
        "update",
        "generate caption from filename and existing metadata",
        generated_caption=generated_caption,
        file_number=file_number,
        file_title=file_title,
    )


def _generated_plan_lines(plan: _GeneratedCaptionPlan) -> list[str]:
    lines = [
        f"#{plan.item.id} [{plan.item.status}] action: {plan.action} - {plan.reason}",
        f"  current: {plan.item.raw_caption or '(none)'}",
        f"  filename: {plan.item.file_name or '(none)'}",
    ]
    if plan.generated_caption is not None:
        lines.append(f"  proposed: {plan.generated_caption}")
    return lines


def _args_raw(message: Message, command: str) -> str:
    return message.text.replace(command, "", 1).strip()


def _args(message: Message, command: str) -> list[str]:
    return _args_raw(message, command).split()


def _assignment_args(message: Message, command: str) -> list[str]:
    raw = _args_raw(message, command)
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) == 1:
        parts = raw.split()
    return parts


def _teacher_id_arg(raw: str) -> tuple[Optional[int], Optional[str]]:
    value = raw.strip().lower()
    if value in {"none", "0", "-"}:
        return None, None
    if not value.isdigit() or int(value) <= 0:
        return None, "Teacher ID must be a positive number, 0, or none."
    return int(value), None


def _positive_ascii_id(raw: str) -> Optional[int]:
    value = raw.strip()
    if not value.isascii() or not value.isdigit():
        return None
    number = int(value)
    if number < 1 or number > _MAX_DB_ID:
        return None
    return number


def _bulk_item_ids(raw: str) -> tuple[Optional[list[int]], Optional[str]]:
    text = raw.strip()
    if not text:
        return None, "Provide at least one item ID."
    tokens = [token.strip() for token in text.split(",")]
    if len(tokens) > MAX_BULK_ITEMS:
        return None, f"Too many item IDs; maximum is {MAX_BULK_ITEMS}."
    if any(not token for token in tokens):
        return None, "Item IDs must be positive numbers separated by commas."

    item_ids: list[int] = []
    invalid: list[str] = []
    for token in tokens:
        item_id = _positive_ascii_id(token)
        if item_id is None:
            invalid.append(token)
        else:
            item_ids.append(item_id)
    if invalid:
        shown = ", ".join(invalid[:20])
        if len(invalid) > 20:
            shown += f" (+{len(invalid) - 20} more)"
        return None, f"Invalid item IDs: {shown}."

    duplicates: list[int] = []
    seen: set[int] = set()
    for item_id in item_ids:
        if item_id in seen and item_id not in duplicates:
            duplicates.append(item_id)
        seen.add(item_id)
    if duplicates:
        return None, "Duplicate item IDs: " + ", ".join(
            str(item_id) for item_id in duplicates
        ) + "."
    return item_ids, None


def _bulk_teacher_id_arg(raw: str) -> tuple[Optional[int], Optional[str]]:
    teacher_id = _positive_ascii_id(raw)
    if teacher_id is None:
        return None, (
            "Teacher ID must be a positive number. "
            "Use /setteacher to clear a teacher."
        )
    return teacher_id, None


async def _teacher_line(session, item) -> str:
    if not item.teacher_id:
        return "teacher: not specified"
    teacher = await session.get(Teacher, item.teacher_id)
    if teacher is None:
        return f"teacher: missing (id {item.teacher_id})"
    return f"teacher: #{teacher.id} {teacher.name}"


async def _topic_line(session, item) -> str:
    topics = await list_item_topics(session, item.id)
    if topics:
        label = ", ".join(f"#{topic.id} {topic.name}" for topic in topics)
    else:
        label = "not assigned"
    if item.topic_review_reason:
        label += f" | review: {item.topic_review_reason}"
    return f"topics: {label}"


def _int_list(raw: str) -> tuple[Optional[list[int]], Optional[str]]:
    values: list[int] = []
    for part in raw.split(","):
        value = part.strip()
        if not value.isdigit() or int(value) <= 0:
            return None, f"Invalid numeric ID: {value or '(blank)'}."
        number = int(value)
        if number not in values:
            values.append(number)
    if not values:
        return None, "At least one ID is required."
    return values, None


def _item_ids(raw: str) -> tuple[Optional[list[int]], Optional[str]]:
    return _int_list(raw)


def _item_line(item) -> str:
    number = f"L{item.lecture_number}" if item.lecture_number is not None else "L?"
    flags = []
    if item.duplicate_flag:
        flags.append(f"dup of #{item.duplicate_of_item_id}")
    if item.review_reason:
        flags.append(item.review_reason)
    if (
        item.teacher_review_reason
        and item.teacher_review_reason not in (item.review_reason or "")
    ):
        flags.append(item.teacher_review_reason)
    suffix = f" | {'; '.join(flags)}" if flags else ""
    return f"#{item.id} [{item.status}] {number} {item.title or 'Untitled'}{suffix}"


async def _course_line(session, item) -> str:
    if not item.course_id:
        return "course: not matched"
    course = await get_course(session, item.course_id)
    if not course:
        return "course: missing"
    destination = course.destination_chat_id or "destination not set"
    topic = course.topic_id if course.topic_id is not None else "topic not set"
    subject = getattr(course, "subject", None)
    subject_name = subject.name if subject else "?"
    label = f"{course.year} / {course.name}" if course.year else course.name
    return f"course: {subject_name} / {label} -> {destination} (topic {topic})"


@router.message(Command("review"))
async def review_cmd(message: Message, session):
    if not _authorized(message):
        return
    batches = await list_batches(session, limit=15)
    if not batches:
        await message.answer("No upload batches yet.")
        return

    lines = ["Upload batches (latest 15):"]
    for batch in batches:
        items = await get_batch_items(session, batch.id)
        needs_review = sum(1 for item in items if item.status in {"needs_review", "failed", "paused"})
        lines.append(
            f"#{batch.id} {batch.batch_date} [{batch.status}] "
            f"items={len(items)} unresolved={needs_review}"
        )
    lines.append("\nUse /batch <id> or /dryrun <id>.")
    await _reply_lines(message, lines)


@router.message(Command("batch"))
async def batch_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/batch")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /batch <batch_id>")
        return
    batch = await get_batch(session, int(args[0]))
    if not batch:
        await message.answer("Batch not found.")
        return

    items = await get_batch_items(session, batch.id)
    lines = [f"Batch #{batch.id} · {batch.batch_date} · {batch.status} · items={len(items)}"]
    if not items:
        lines.append("No items.")
    for item in items:
        lines.append(_item_line(item))
        lines.append("  " + await _course_line(session, item))
        lines.append("  " + await _teacher_line(session, item))
        lines.append("  " + await _topic_line(session, item))
    await _reply_lines(message, lines)


@router.message(Command("dryrun"))
async def dryrun_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/dryrun")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /dryrun <batch_id>")
        return
    batch = await get_batch(session, int(args[0]))
    if not batch:
        await message.answer("Batch not found.")
        return

    items = await get_batch_items(session, batch.id)
    lines = [
        f"DRY RUN · batch #{batch.id} · nothing will be published",
        f"Items sorted by lecture number ({len(items)}):",
    ]
    for item in items:
        lines.append(_item_line(item))
        lines.append("  " + await _course_line(session, item))
        lines.append("  " + await _teacher_line(session, item))
        lines.append("  " + await _topic_line(session, item))
    await _reply_lines(message, lines)


@router.message(Command("refreshcaptions"))
async def refreshcaptions_cmd(message: Message, session, bot: Bot):
    if not _authorized(message):
        return
    args = _args(message, "/refreshcaptions")
    if (
        len(args) not in {1, 2}
        or not args[0].isdigit()
        or (len(args) == 2 and args[1].casefold() != "apply")
    ):
        await message.answer(
            "Usage: /refreshcaptions <batch_id> [apply]\n"
            "Preview without apply. Apply updates invalid or non-preferred "
            "captions, fills lecture numbers from numbered captions, and "
            "reclassifies already-valid ones. It never publishes or creates "
            "uploads.",
            parse_mode=None,
        )
        return

    apply_changes = len(args) == 2
    batch = await get_batch(session, int(args[0]))
    if not batch:
        await message.answer("Batch not found.")
        return
    batch_id = batch.id
    if apply_changes and batch.status != "open":
        await message.answer(
            f"Batch #{batch.id} is {batch.status}; no changes were applied.",
            parse_mode=None,
        )
        return

    items = await get_batch_items(session, batch.id)
    if not items:
        await message.answer("Batch has no items.", parse_mode=None)
        return

    admin_chat_id = message.from_user.id
    plans: list[_CaptionRefreshPlan] = []
    for item in items:
        source_caption, source_error = await _fetch_source_caption(
            bot, admin_chat_id, item
        )
        plans.append(
            _caption_refresh_plan(item, source_caption, source_error)
        )

    if not apply_changes:
        lines = [
            f"Caption refresh preview - batch #{batch.id} ({len(items)} items). "
            "No database changes made.",
            "Add 'apply' to update non-preferred captions, fill lecture "
            "numbers from numbered captions, and reclassify valid ones. "
            "No messages will be published.",
        ]
        for plan in plans:
            lines.extend(_refresh_plan_lines(plan))
        await _reply_plain_lines(message, lines)
        return

    updated = 0
    reclassified = 0
    preserved = 0
    skipped = 0
    failed = 0
    lines: list[str] = []

    for plan in plans:
        item = plan.item
        if plan.action == "preserve":
            preserved += 1
            lines.append(
                f"#{item.id}: kept current preferred caption"
                f"{_source_unavailable_note(plan)}"
            )
            continue
        if plan.action == "skip":
            skipped += 1
            lines.append(f"#{item.id}: skipped - {plan.reason}")
            continue

        item_id = item.id
        try:
            if plan.action == "update":
                refreshed, outcome = await update_ingested_caption(
                    session,
                    source_chat_id=item.source_chat_id,
                    source_message_id=item.source_message_id,
                    caption=plan.replacement_caption or plan.source_caption,
                )
            else:
                refreshed, outcome = await reclassify_item_caption(
                    session, item.id
                )
        except Exception as exc:
            await session.rollback()
            logger.exception(
                "Caption refresh failed for item #%s in batch #%s",
                item_id,
                batch_id,
            )
            failed += 1
            lines.append(
                f"#{item_id}: failed - {_telegram_error_text(exc)}"
            )
            continue

        if outcome == "updated":
            updated += 1
            lines.append(
                f"#{item.id}: caption updated and reclassified -> "
                f"[{refreshed.status}] {_number_label(refreshed.lecture_number)} "
                f"{refreshed.title or 'Untitled'}"
            )
        elif outcome == "reclassified":
            reclassified += 1
            lines.append(
                f"#{item.id}: current caption reclassified -> "
                f"[{refreshed.status}] {_number_label(refreshed.lecture_number)} "
                f"{refreshed.title or 'Untitled'}"
                f"{_source_unavailable_note(plan)}"
            )
        elif outcome == "unchanged":
            preserved += 1
            lines.append(f"#{item.id}: caption already up to date")
        elif outcome == "failed":
            failed += 1
            lines.append(
                f"#{item.id}: failed - classification error recorded"
            )
        else:
            skipped += 1
            lines.append(f"#{item.id}: skipped - {outcome}")

    header = (
        f"Caption refresh applied to batch #{batch.id}: {updated} updated, "
        f"{reclassified} reclassified, {preserved} preserved, "
        f"{skipped} skipped, {failed} failed."
    )
    await _reply_plain_lines(message, [header, *lines])


@router.message(Command("generatecaptions"))
async def generatecaptions_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/generatecaptions")
    if (
        len(args) not in {1, 2}
        or not args[0].isdigit()
        or (len(args) == 2 and args[1].casefold() != "apply")
    ):
        await message.answer(
            "Usage: /generatecaptions <batch_id> [apply]\n"
            "Preview generated pipe captions from filenames and existing "
            "course/teacher/topic metadata. Apply updates only items that do "
            "not already have a valid caption. It never approves or publishes.",
            parse_mode=None,
        )
        return

    apply_changes = len(args) == 2
    batch = await get_batch(session, int(args[0]))
    if not batch:
        await message.answer("Batch not found.")
        return
    if apply_changes and batch.status != "open":
        await message.answer(
            f"Batch #{batch.id} is {batch.status}; no changes were applied.",
            parse_mode=None,
        )
        return

    items = await get_batch_items(session, batch.id)
    if not items:
        await message.answer("Batch has no items.")
        return

    plans: list[_GeneratedCaptionPlan] = []
    for item in items:
        plans.append(await _generated_caption_plan(session, item))

    if not apply_changes:
        lines = [
            f"Caption generation preview - batch #{batch.id} ({len(items)} items). "
            "No database changes made.",
            "Add 'apply' to update items without a valid caption. Valid "
            "captions are preserved. No messages will be published.",
        ]
        for plan in plans:
            lines.extend(_generated_plan_lines(plan))
        await _reply_plain_lines(message, lines)
        return

    updated = 0
    preserved = 0
    skipped = 0
    failed = 0
    lines: list[str] = []

    for plan in plans:
        item = plan.item
        if plan.action == "preserve":
            preserved += 1
            lines.append(f"#{item.id}: kept current valid caption")
            continue
        if plan.action == "skip":
            skipped += 1
            lines.append(f"#{item.id}: skipped - {plan.reason}")
            continue

        item_id = item.id
        try:
            refreshed, error = await set_item_caption(
                session, item.id, plan.generated_caption
            )
        except ApprovedBatchError as exc:
            await session.rollback()
            skipped += 1
            lines.append(f"#{item_id}: skipped - {exc}")
            continue
        except Exception as exc:
            await session.rollback()
            logger.exception(
                "Caption generation failed for item #%s in batch #%s",
                item_id,
                batch_id,
            )
            failed += 1
            lines.append(f"#{item_id}: failed - {exc}")
            continue

        if error:
            failed += 1
            lines.append(f"#{item.id}: failed - {error}")
            continue
        if refreshed is None:
            skipped += 1
            lines.append(f"#{item.id}: skipped")
            continue

        updated += 1
        lines.append(
            f"#{item.id}: caption generated -> {_item_line(refreshed)}"
        )

    header = (
        f"Caption generation applied to batch #{batch.id}: {updated} updated, "
        f"{preserved} preserved, {skipped} skipped, {failed} failed."
    )
    await _reply_plain_lines(message, [header, *lines])


@dataclass
class _TeacherPlan:
    item: UploadItem
    action: str
    reason: str
    current_teacher: Optional[Teacher] = None
    proposed_teacher: Optional[Teacher] = None


def _teacher_label(teacher: Optional[Teacher]) -> str:
    if teacher is None:
        return "not specified"
    return f"#{teacher.id} {teacher.name}"


def _teacher_plan_scope(raw: str):
    raw = raw.strip()
    if raw.startswith("batch:"):
        value = raw[len("batch:"):].strip()
        if value.isdigit() and int(value) > 0:
            return ("batch", int(value)), None
        return None, "Batch ID must be a positive number."
    if raw.startswith("items:"):
        value = raw[len("items:"):].strip()
        item_ids, error = _bulk_item_ids(value)
        if error:
            return None, error
        return ("items", item_ids), None
    return None, "Scope must be batch:<id> or items:<id,id,...>."


def _teacher_spec(raw: str):
    raw = raw.strip()
    if not raw:
        return None, "Teacher ID or name is required."
    if raw.casefold() == "none":
        return None, "Use /setteacher to clear a teacher."
    if raw.isdigit():
        teacher_id = int(raw)
        if teacher_id <= 0:
            return None, "Teacher ID must be a positive number."
        return ("id", teacher_id), None
    return ("name", raw), None


async def _teacher_change_blocker(session, item: UploadItem) -> Optional[str]:
    if item.status == "skipped":
        return "item is skipped"
    if (
        item.status in CAPTION_EDIT_BLOCKED_STATUSES
        or item.publication_status in {"publishing", "published"}
    ):
        return "item is publishing, published, or otherwise locked"
    batch = await get_batch(session, item.batch_id)
    if batch is not None and batch.status == "approved":
        return "batch is approved"
    return None


async def _teacher_plan(
    session, item: UploadItem, spec: tuple
) -> _TeacherPlan:
    current_teacher = None
    if item.teacher_id is not None:
        current_teacher = await session.get(Teacher, item.teacher_id)

    blocker = await _teacher_change_blocker(session, item)
    if blocker:
        return _TeacherPlan(
            item, "skip", blocker, current_teacher=current_teacher
        )

    course = None
    if item.course_id is not None:
        course = await get_course(session, item.course_id)
    if course is None or course.subject is None:
        return _TeacherPlan(
            item,
            "skip",
            "course/subject missing; cannot resolve a subject-scoped teacher",
            current_teacher=current_teacher,
        )

    subject_id = course.subject_id
    if spec[0] == "id":
        teacher = await session.get(Teacher, spec[1])
        if teacher is None:
            return _TeacherPlan(
                item,
                "missing",
                f"teacher not found: #{spec[1]}",
                current_teacher=current_teacher,
            )
        if teacher.subject_id != subject_id:
            return _TeacherPlan(
                item,
                "missing",
                (
                    f"teacher #{teacher.id} {teacher.name} belongs to a "
                    "different subject"
                ),
                current_teacher=current_teacher,
            )
        proposed = teacher
    else:
        matches = await find_matching_teachers(session, subject_id, spec[1])
        if not matches:
            return _TeacherPlan(
                item,
                "missing",
                f"teacher not found: {spec[1]}",
                current_teacher=current_teacher,
            )
        if len(matches) > 1:
            labels = ", ".join(
                f"#{teacher.id} {teacher.name}" for teacher in matches
            )
            return _TeacherPlan(
                item,
                "ambiguous",
                f"ambiguous teacher: {spec[1]} ({labels})",
                current_teacher=current_teacher,
            )
        proposed = matches[0]

    if current_teacher is not None and current_teacher.id == proposed.id:
        return _TeacherPlan(
            item,
            "preserve",
            "teacher is already assigned",
            current_teacher=current_teacher,
            proposed_teacher=proposed,
        )

    return _TeacherPlan(
        item,
        "update",
        "assign proposed teacher",
        current_teacher=current_teacher,
        proposed_teacher=proposed,
    )


def _teacher_plan_lines(plan: _TeacherPlan) -> list[str]:
    current = _teacher_label(plan.current_teacher)
    proposed = (
        _teacher_label(plan.proposed_teacher)
        if plan.proposed_teacher is not None
        else "(none)"
    )
    return [
        f"#{plan.item.id} [{plan.item.status}] current: {current} -> "
        f"proposed: {proposed} - {plan.action}: {plan.reason}"
    ]


@router.message(Command("setteacherplan"))
async def setteacherplan_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = _args_raw(message, "/setteacherplan")
    apply_changes = False
    if raw.casefold().endswith(" apply"):
        apply_changes = True
        raw = raw[:-6].strip()

    parts = [part.strip() for part in raw.split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await message.answer(
            "Usage: /setteacherplan <batch:<id>|items:<id,id,...>>|"
            "<teacher_id|teacher name> [apply]\n"
            "Example: /setteacherplan batch:7|Dr New Teacher\n"
            "Example: /setteacherplan items:55,56|4 apply\n"
            "Preview first. Apply assigns existing teachers and flags "
            "missing/ambiguous teachers for review. It never approves or "
            "publishes.",
            parse_mode=None,
        )
        return

    scope, error = _teacher_plan_scope(parts[0])
    if error:
        await message.answer(error, parse_mode=None)
        return
    spec, error = _teacher_spec(parts[1])
    if error:
        await message.answer(error, parse_mode=None)
        return

    batch = None
    missing_item_ids: list[int] = []
    if scope[0] == "batch":
        batch = await get_batch(session, scope[1])
        if batch is None:
            await message.answer("Batch not found.", parse_mode=None)
            return
        items = await get_batch_items(session, batch.id)
        scope_label = f"batch #{batch.id}"
    else:
        items = []
        for item_id in scope[1]:
            item = await session.get(UploadItem, item_id)
            if item is None:
                missing_item_ids.append(item_id)
            else:
                items.append(item)
        if missing_item_ids:
            await message.answer(
                "Item not found: " + ", ".join(map(str, missing_item_ids)),
                parse_mode=None,
            )
            return
        scope_label = "selected items"

    if not items:
        await message.answer("No items to update.", parse_mode=None)
        return

    plans: list[_TeacherPlan] = []
    for item in items:
        plans.append(await _teacher_plan(session, item, spec))

    if not apply_changes:
        lines = [
            f"Teacher assignment preview - {scope_label} ({len(items)} items). "
            "No database changes made.",
            "Add 'apply' to assign the proposed teacher. Missing or "
            "ambiguous teachers are flagged for review. Subject and topic "
            "assignments are unchanged. No messages will be published.",
        ]
        for plan in plans:
            lines.extend(_teacher_plan_lines(plan))
        await _reply_plain_lines(message, lines)
        return

    if batch is not None and batch.status != "open":
        await message.answer(
            f"Batch #{batch.id} is {batch.status}; no changes were applied.",
            parse_mode=None,
        )
        return

    updated = 0
    preserved = 0
    skipped = 0
    flagged = 0
    failed = 0
    lines: list[str] = []

    updates_by_teacher: dict[int, list[_TeacherPlan]] = {}
    flags: list[_TeacherPlan] = []
    for plan in plans:
        if plan.action == "update":
            updates_by_teacher.setdefault(
                plan.proposed_teacher.id, []
            ).append(plan)
        elif plan.action in {"missing", "ambiguous"}:
            flags.append(plan)
        elif plan.action == "preserve":
            preserved += 1
            lines.append(f"#{plan.item.id}: teacher already assigned")
        else:
            skipped += 1
            lines.append(f"#{plan.item.id}: skipped - {plan.reason}")

    for teacher_id, group in updates_by_teacher.items():
        group_ids = [plan.item.id for plan in group]
        try:
            teacher, assigned, errors = await set_items_teacher_bulk(
                session, group_ids, teacher_id
            )
        except Exception:
            await session.rollback()
            logger.exception(
                "Bulk teacher assignment failed for teacher #%s", teacher_id
            )
            failed += len(group)
            for item_id in group_ids:
                lines.append(f"#{item_id}: failed - bulk assignment error")
            continue

        if errors:
            failed += len(group)
            for plan in group:
                lines.append(f"#{plan.item.id}: failed - {errors[0]}")
            continue

        assert teacher is not None
        updated += len(assigned)
        for plan in group:
            lines.append(
                f"#{plan.item.id}: teacher set to "
                f"#{teacher.id} {teacher.name}"
            )

    for plan in flags:
        flagged_item = await flag_item_teacher_review(
            session, plan.item.id, plan.reason
        )
        if flagged_item is None:
            failed += 1
            lines.append(f"#{plan.item.id}: failed - item not found")
            continue
        flagged += 1
        lines.append(f"#{plan.item.id}: flagged - {plan.reason}")

    header = (
        f"Teacher assignment applied: {updated} updated, "
        f"{preserved} preserved, {flagged} flagged, {skipped} skipped, "
        f"{failed} failed."
    )
    await _reply_plain_lines(message, [header, *lines])


@router.message(Command("approve"))
async def approve_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/approve")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /approve <batch_id>")
        return
    ok, text = await approve_batch(session, int(args[0]), message.from_user.id)
    await message.answer(text)


@router.message(Command("createbatch"))
async def createbatch_cmd(message: Message, session):
    if not _authorized(message):
        return
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Usage: /createbatch <name>")
        return
    from datetime import datetime, timezone
    from bot import config

    config.get_settings.cache_clear()
    settings = config.get_settings()
    try:
        source_chat_id = int(settings.upload_inbox_chat_id)
    except (TypeError, ValueError):
        await message.answer("UPLOAD_INBOX_CHAT_ID is not configured.")
        return
    today = datetime.now(timezone.utc).date()
    open_batch = await get_open_batch_for_date(session, source_chat_id, today)
    if open_batch is not None:
        # Only one batch per chat/date may be "open". Keep the current upload
        # target untouched and create the new batch as a ready-to-fill one.
        batch = await create_named_batch(
            session, source_chat_id, today, parts[1].strip(), status="collection_finished"
        )
        await message.answer(
            f"Created batch #{batch.id} ({batch.name or 'unnamed'}) "
            f"status={batch.status}.\n"
            f"Batch #{open_batch.id} is still the automatic upload target. "
            f"Use /startbatch {batch.id} to send new uploads here."
        )
        return
    batch = await create_named_batch(session, source_chat_id, today, parts[1].strip())
    await message.answer(f"Created batch #{batch.id} ({batch.name or 'unnamed'}) status={batch.status}")


@router.message(Command("startbatch"))
async def startbatch_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/startbatch")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /startbatch <batch_id>")
        return
    from bot import config

    config.get_settings.cache_clear()
    settings = config.get_settings()
    try:
        source_chat_id = int(settings.upload_inbox_chat_id)
    except (TypeError, ValueError):
        await message.answer("UPLOAD_INBOX_CHAT_ID is not configured.")
        return
    batch, error = await start_collecting_batch(session, int(args[0]), source_chat_id)
    if error:
        await message.answer(error)
        return
    await message.answer(f"Started collecting into batch #{batch.id} ({batch.name or 'unnamed'}).")


@router.message(Command("finishbatch"))
async def finishbatch_cmd(message: Message, session):
    if not _authorized(message):
        return
    from bot import config

    config.get_settings.cache_clear()
    settings = config.get_settings()
    try:
        source_chat_id = int(settings.upload_inbox_chat_id)
    except (TypeError, ValueError):
        await message.answer("UPLOAD_INBOX_CHAT_ID is not configured.")
        return
    batch, error = await finish_collecting_batch(session, source_chat_id)
    if error:
        await message.answer(error)
        return
    await message.answer(f"Finished collection for batch #{batch.id} ({batch.name or 'unnamed'}). No items were approved or published.")


@router.message(Command("activebatch"))
async def activebatch_cmd(message: Message, session):
    if not _authorized(message):
        return
    from bot import config

    config.get_settings.cache_clear()
    settings = config.get_settings()
    try:
        source_chat_id = int(settings.upload_inbox_chat_id)
    except (TypeError, ValueError):
        await message.answer("UPLOAD_INBOX_CHAT_ID is not configured.")
        return
    batch = await get_active_collecting_batch(session, source_chat_id)
    if not batch:
        await message.answer("No batch is currently collecting.")
        return
    items = await get_batch_items(session, batch.id)
    await message.answer(f"Active collecting batch #{batch.id} ({batch.name or 'unnamed'}) status={batch.status}, items={len(items)}.")


def _parse_item_ids(raw: str) -> tuple[list[int], Optional[str]]:
    """Parse ``50-60,65,70-72`` into a de-duplicated list of ids."""
    ids: list[int] = []
    seen: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_raw, _, end_raw = chunk.partition("-")
            if not start_raw.strip().isdigit() or not end_raw.strip().isdigit():
                return [], f"Invalid range '{chunk}'."
            start, end = int(start_raw), int(end_raw)
            if start > end:
                return [], f"Invalid range '{chunk}': start is after end."
            if end - start > 500:
                return [], f"Range '{chunk}' is too large (max 500 ids)."
            candidates = range(start, end + 1)
        elif chunk.isdigit():
            candidates = (int(chunk),)
        else:
            return [], f"Invalid id '{chunk}'."
        for value in candidates:
            if value not in seen:
                seen.add(value)
                ids.append(value)
    if not ids:
        return [], "No item ids given."
    return ids, None


async def _move_items_to_batch_reply(
    message: Message, session, command: str, raw: str
) -> None:
    usage = f"Usage: {command} <id,id,start-end,...>|<batch_id>"
    parts = raw.split("|")
    if len(parts) != 2:
        await message.answer(usage)
        return
    ids_raw, batch_raw = (part.strip() for part in parts)
    if not batch_raw.isdigit():
        await message.answer(usage)
        return
    item_ids, error = _parse_item_ids(ids_raw)
    if error:
        await message.answer(f"{error}\n{usage}")
        return
    batch, moved, errors = await move_items_to_batch(session, item_ids, int(batch_raw))
    if batch is None:
        await message.answer("\n".join(errors) if errors else "Batch not found.")
        return
    if errors:
        await message.answer(
            f"Batch #{batch.id}: nothing moved.\n" + "\n".join(errors),
            parse_mode=None,
        )
        return
    await message.answer(
        f"Moved {len(moved)} item(s) into batch #{batch.id} "
        f"({batch.name or 'unnamed'}). They are not approved or published.",
        parse_mode=None,
    )


@router.message(Command("moveitem"))
async def moveitem_cmd(message: Message, session):
    if not _authorized(message):
        return
    await _move_items_to_batch_reply(message, session, "/moveitem", _args_raw(message, "/moveitem"))


@router.message(Command("moveitems"))
async def moveitems_cmd(message: Message, session):
    if not _authorized(message):
        return
    await _move_items_to_batch_reply(message, session, "/moveitems", _args_raw(message, "/moveitems"))


@router.message(Command("publish"))
async def publish_cmd(message: Message, session, bot: Bot):
    if not _authorized(message):
        return
    args = _args(message, "/publish")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /publish <batch_id>")
        return

    result = await publish_batch(session, bot, int(args[0]))
    if result.error:
        await message.answer(result.error)
        return

    lines = [
        f"Batch #{result.batch_id} publish: {result.published} published, "
        f"{result.already_published} already published, {result.skipped} skipped, "
        f"{len(result.failures)} failed."
    ]
    for failure in result.failures:
        lines.append(f"#{failure.item_id}: {failure.reason}")
    await _reply_lines(message, lines)


@router.message(Command("unlockpublish"))
async def unlockpublish_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/unlockpublish")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /unlockpublish <item_id>")
        return
    item, error = await unlock_publish_state(session, int(args[0]))
    if error:
        await message.answer(error)
        return
    await message.answer(
        f"Item #{item.id} unlocked. Verify the destination topic before "
        "running /publish again."
    )


@router.message(Command("reconcilepublish"))
async def reconcilepublish_cmd(message: Message, session):
    if not _authorized(message):
        return
    parts = [
        part.strip()
        for part in _args_raw(message, "/reconcilepublish").split("|")
    ]
    if len(parts) != 4:
        await message.answer(
            "Usage: /reconcilepublish "
            "<item_id>|<message_id>|<destination_chat_id>|<topic_id>"
        )
        return

    item_raw, message_raw, destination, topic_raw = parts
    if (
        not item_raw.isdigit()
        or not message_raw.isdigit()
        or not topic_raw.isdigit()
    ):
        await message.answer(
            "Item ID, message ID, and topic ID must be positive integers."
        )
        return

    item, error = await reconcile_publish_state(
        session,
        int(item_raw),
        message_id=int(message_raw),
        destination_chat_id=destination,
        topic_id=int(topic_raw),
    )
    if error:
        await message.answer(error)
        return

    await message.answer(
        f"Item #{item.id} reconciled as published (message {item.published_message_id}). "
        "No message was sent."
    )


@router.message(Command("skip"))
async def skip_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/skip")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /skip <item_id>")
        return
    try:
        skipped = await skip_item(session, int(args[0]))
    except ApprovedBatchError as exc:
        await message.answer(str(exc))
        return
    if skipped:
        await message.answer(f"Item #{args[0]} skipped.")
    else:
        await message.answer("Item not found.")


@router.message(Command("keep"))
async def keep_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/keep")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /keep <item_id>")
        return
    try:
        item = await keep_item(session, int(args[0]))
    except ApprovedBatchError as exc:
        await message.answer(str(exc))
        return
    if not item:
        await message.answer("Item not found.")
        return
    await message.answer(f"Duplicate flag cleared.\n{_item_line(item)}")


@router.message(Command("retry"))
async def retry_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/retry")
    if not args or not args[0].isdigit():
        await message.answer("Usage: /retry <item_id>")
        return
    try:
        item = await retry_item(session, int(args[0]))
    except ApprovedBatchError as exc:
        await message.answer(str(exc))
        return
    if not item:
        await message.answer("Item not found.")
        return
    await message.answer(f"Item reclassified.\n{_item_line(item)}")


@router.message(Command("setcourse"))
async def setcourse_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/setcourse")
    if len(args) < 2 or not all(arg.isdigit() for arg in args[:2]):
        await message.answer("Usage: /setcourse <item_id> <course_id>")
        return
    try:
        item, error = await set_item_course(session, int(args[0]), int(args[1]))
    except ApprovedBatchError as exc:
        await message.answer(str(exc))
        return
    if error:
        await message.answer(error)
        return
    await message.answer(f"Course set.\n{_item_line(item)}")


@router.message(Command("setnumber"))
async def setnumber_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/setnumber")
    if len(args) < 2 or not all(arg.isdigit() for arg in args[:2]):
        await message.answer("Usage: /setnumber <item_id> <lecture_number>")
        return
    try:
        item, error = await set_item_number(session, int(args[0]), int(args[1]))
    except ApprovedBatchError as exc:
        await message.answer(str(exc))
        return
    if error:
        await message.answer(error)
        return
    await message.answer(f"Lecture number set.\n{_item_line(item)}")


@router.message(Command("addteacher"))
async def addteacher_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = message.text.replace("/addteacher", "", 1).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await message.answer(
            "Usage: /addteacher <subject_slug>|<name>|[alias1,alias2]\n"
            "Example: /addteacher anatomy|Dr A. Sharma\n"
            "Example with aliases: /addteacher anatomy|Dr. Muhammad Azam|"
            "Dr Mohammad Azam,Mohammad Azam"
        )
        return
    subject_slug, name = parts[0], parts[1]
    aliases = (
        [alias.strip() for alias in parts[2].split(",") if alias.strip()]
        if len(parts) > 2
        else None
    )
    subject = await get_subject(session, subject_slug)
    if not subject:
        await message.answer(
            f"Subject '{subject_slug}' not found. Use /subjects to see the available subjects."
        )
        return
    teacher, error = await add_teacher(
        session, subject.id, name, aliases=aliases
    )
    if error:
        await message.answer(error)
        return
    await message.answer(
        f"Added teacher #{teacher.id}: {teacher.name} ({subject.name})"
    )


@router.message(Command("teacheralias"))
async def teacheralias_cmd(message: Message, session):
    if not _authorized(message):
        return
    parts = [part.strip() for part in _args_raw(message, "/teacheralias").split("|")]
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1]:
        await message.answer(
            "Usage: /teacheralias <teacher_id>|<alias>\n"
            "Example: /teacheralias 4|Dr Mohammad Azam"
        )
        return
    teacher_id = int(parts[0])
    alias, error = await add_teacher_alias(session, teacher_id, parts[1])
    if error:
        await message.answer(error)
        return
    assert alias is not None
    await message.answer(f"Added alias '{alias.name}' for teacher #{teacher_id}.")


@router.message(Command("removealias"))
async def removealias_cmd(message: Message, session):
    if not _authorized(message):
        return
    parts = [part.strip() for part in _args_raw(message, "/removealias").split("|")]
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1]:
        await message.answer("Usage: /removealias <teacher_id>|<alias>")
        return
    teacher_id = int(parts[0])
    _, error = await remove_teacher_alias(session, teacher_id, parts[1])
    if error:
        await message.answer(error)
        return
    await message.answer(f"Removed alias from teacher #{teacher_id}.")


@router.message(Command("renameteacher"))
async def renameteacher_cmd(message: Message, session):
    if not _authorized(message):
        return
    parts = [part.strip() for part in _args_raw(message, "/renameteacher").split("|")]
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1]:
        await message.answer(
            "Usage: /renameteacher <teacher_id>|<new name>\n"
            "The teacher ID and all uploaded/lecture links are preserved."
        )
        return
    teacher_id = int(parts[0])
    teacher, error = await rename_teacher(session, teacher_id, parts[1])
    if error:
        await message.answer(error)
        return
    assert teacher is not None
    await message.answer(f"Renamed teacher #{teacher.id} to {teacher.name}.")


@router.message(Command("teachers"))
async def teachers_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/teachers")
    if not args:
        await message.answer("Usage: /teachers <subject_slug>")
        return
    subject = await get_subject(session, args[0])
    if not subject:
        await message.answer("Subject not found.")
        return
    teachers = await list_teachers(session, subject.id)
    if not teachers:
        await message.answer(
            f"No teachers for {subject.name}. Use /addteacher {subject.slug}|<name>."
        )
        return
    lines = [f"Teachers for {subject.name}:"]
    for teacher in teachers:
        lines.append(f"#{teacher.id} {teacher.name}")
        aliases = await list_teacher_aliases(session, teacher.id)
        if aliases:
            lines.append(
                "  aliases: " + ", ".join(alias.name for alias in aliases)
            )
    await _reply_lines(message, lines)


@router.message(Command("setteacher"))
async def setteacher_cmd(message: Message, session):
    if not _authorized(message):
        return
    parts = _assignment_args(message, "/setteacher")
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer(
            "Usage: /setteacher <item_id>|<teacher_id|none>"
        )
        return
    teacher_id, error = _teacher_id_arg(parts[1])
    if error:
        await message.answer(error)
        return
    item, error = await set_item_teacher(session, int(parts[0]), teacher_id)
    if error:
        await message.answer(error)
        return
    if teacher_id is None:
        await message.answer(f"Teacher cleared for item #{item.id}.")
        return
    teacher = await session.get(Teacher, teacher_id)
    await message.answer(
        f"Teacher set to #{teacher.id} {teacher.name} for item #{item.id}."
    )


@router.message(Command("setteacherbulk"))
async def setteacherbulk_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = _args_raw(message, "/setteacherbulk")
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await message.answer(
            "Usage: /setteacherbulk <item_ids>|<teacher_id>\n"
            "Example: /setteacherbulk 7,9,8|1"
        )
        return

    item_ids, error = _bulk_item_ids(parts[0])
    if error:
        await message.answer(error)
        return
    teacher_id, error = _bulk_teacher_id_arg(parts[1])
    if error:
        await message.answer(error)
        return

    try:
        teacher, items, errors = await set_items_teacher_bulk(
            session, item_ids, teacher_id
        )
    except Exception:
        logger.exception("Bulk teacher assignment failed")
        await message.answer("Bulk assignment failed. No changes were applied.")
        return

    if errors:
        lines = ["No changes were applied."]
        if teacher is None:
            lines.extend(errors)
        else:
            lines.append(
                f"Could not assign teacher #{teacher.id} {teacher.name}:"
            )
            lines.extend(errors)
        await _reply_lines(message, lines)
        return

    assert teacher is not None
    noun = "item" if len(items) == 1 else "items"
    await _reply_lines(
        message,
        [
            f"Assigned teacher #{teacher.id} {teacher.name} "
            f"to {len(items)} {noun}.",
            "Items: " + ", ".join(str(item.id) for item in items),
        ],
    )


@router.message(Command("addtopic"))
async def addtopic_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = message.text.replace("/addtopic", "", 1).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        await message.answer(
            "Usage: /addtopic <subject_slug>|<name>\n"
            "Example: /addtopic anatomy|Upper Limb"
        )
        return
    subject_slug, name = parts[0], parts[1]
    subject = await get_subject(session, subject_slug)
    if not subject:
        await message.answer(f"Subject '{subject_slug}' not found.")
        return
    topic, error = await add_topic(session, subject.id, name)
    if error:
        await message.answer(error)
        return
    await message.answer(
        f"Added topic #{topic.id}: {topic.name} ({subject.name})"
    )


@router.message(Command("topics"))
async def topics_cmd(message: Message, session):
    if not _authorized(message):
        return
    args = _args(message, "/topics")
    if not args:
        await message.answer("Usage: /topics <subject_slug>")
        return
    subject = await get_subject(session, args[0])
    if not subject:
        await message.answer("Subject not found.")
        return
    topics = await list_topics(session, subject.id)
    if not topics:
        await message.answer(
            f"No topics for {subject.name}. Use /addtopic {subject.slug}|<name>."
        )
        return
    lines = [f"Topics for {subject.name}:"]
    for topic in topics:
        lines.append(f"#{topic.id} {topic.name}")
    await _reply_lines(message, lines)


@router.message(Command("settopic"))
async def settopic_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = message.text.replace("/settopic", "", 1).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 2:
        await message.answer(
            "Usage: /settopic <item_id[,item_id...]>|<topic_id[,topic_id...]|none>"
        )
        return
    item_ids, error = _item_ids(parts[0])
    if error:
        await message.answer(error)
        return
    topic_raw = parts[1].lower()
    if topic_raw in {"none", "0", "-"}:
        topic_ids = []
    else:
        topic_ids, error = _int_list(parts[1])
        if error:
            await message.answer(error)
            return

    lines = []
    for item_id in item_ids:
        item, error = await set_item_topics(session, item_id, topic_ids)
        if error:
            lines.append(f"#{item_id}: {error}")
        else:
            label = ", ".join(f"#{t.id} {t.name}" for t in await list_item_topics(session, item.id))
            lines.append(f"#{item.id}: topics set to {label or 'none'}")
    await _reply_lines(message, lines)


@router.message(Command("assign"))
async def assign_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = message.text.replace("/assign", "", 1).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 4:
        await message.answer(
            "Usage: /assign <item_ids>|<course_id|->|<topic_ids|none|->|<teacher_id|none|->\n"
            "Example: /assign 12,13|4|1,2|8\n"
            "Use - to leave a field unchanged."
        )
        return
    item_ids, error = _item_ids(parts[0])
    if error:
        await message.answer(error)
        return

    course_raw = parts[1]
    course_id = None
    if course_raw != "-":
        if not course_raw.isdigit() or int(course_raw) <= 0:
            await message.answer("Course ID must be a positive number or -.")
            return
        course_id = int(course_raw)

    topic_raw = parts[2].lower()
    topic_ids = None
    if topic_raw not in {"-", ""}:
        if topic_raw in {"none", "0"}:
            topic_ids = []
        else:
            topic_ids, error = _int_list(parts[2])
            if error:
                await message.answer(error)
                return

    teacher_raw = parts[3].lower()
    teacher_id = None
    if teacher_raw not in {"-", ""}:
        if teacher_raw in {"none", "0"}:
            teacher_id = None
        elif teacher_raw.isdigit() and int(teacher_raw) > 0:
            teacher_id = int(teacher_raw)
        else:
            await message.answer("Teacher ID must be a positive number, none, or -.")
            return

    lines = []
    try:
        for item_id in item_ids:
            item, error = await assign_item(
                session,
                item_id,
                course_id=course_id,
                topic_ids=topic_ids,
                teacher_id=teacher_id,
                teacher_requested=teacher_raw not in {"-", ""},
            )
            if error:
                await session.rollback()
                await message.answer(
                    f"Assignment failed for item #{item_id}: {error}\n"
                    "No changes were applied."
                )
                return
            lines.append(f"#{item_id}: assigned")
        await session.commit()
    except ApprovedBatchError as exc:
        await session.rollback()
        await message.answer(
            f"Assignment failed: {exc}\nNo changes were applied."
        )
        return
    await _reply_lines(message, lines)


@router.message(Command("setlecturetopic"))
async def setlecturetopic_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = message.text.replace("/setlecturetopic", "", 1).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer(
            "Usage: /setlecturetopic <lecture_id>|<topic_id[,topic_id...]|none>"
        )
        return
    topic_raw = parts[1].lower()
    if topic_raw in {"none", "0", "-"}:
        topic_ids = []
    else:
        topic_ids, error = _int_list(parts[1])
        if error:
            await message.answer(error)
            return
    lecture, error = await set_lecture_topics(session, int(parts[0]), topic_ids)
    if error:
        await message.answer(error)
        return
    label = ", ".join(f"#{topic_id}" for topic_id in topic_ids)
    await message.answer(
        f"Lecture #{lecture.id} topics set to {label or 'none'}."
    )


@router.message(Command("setcaption"))
async def setcaption_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = message.text.replace("/setcaption", "", 1).strip()
    parts = raw.split("|", 1)
    if len(parts) < 2 or not parts[0].strip().isdigit():
        await message.answer(
            "Usage: /setcaption <item_id>|<new caption>\n"
            "The previous caption is kept for audit.",
            parse_mode=None,
        )
        return
    try:
        item, error = await set_item_caption(
            session, int(parts[0].strip()), parts[1]
        )
    except ApprovedBatchError as exc:
        await message.answer(str(exc), parse_mode=None)
        return
    if error:
        await message.answer(error, parse_mode=None)
        return
    await message.answer(
        f"Caption updated and item reclassified.\n{_item_line(item)}",
        parse_mode=None,
    )


@router.message(Command("setlecturetitle"))
async def setlecturetitle_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = message.text.replace("/setlecturetitle", "", 1).strip()
    parts = raw.split("|", 1)
    if len(parts) < 2 or not parts[0].strip().isdigit():
        await message.answer(
            "Usage: /setlecturetitle <lecture_id>|<title>\n"
            "This updates the student-visible title without re-uploading or "
            "changing the original caption."
        )
        return
    lecture, error = await set_lecture_title(
        session, int(parts[0].strip()), parts[1]
    )
    if error:
        await message.answer(error)
        return
    await message.answer(
        f"Lecture #{lecture.id} title set to: {lecture.title}"
    )


@router.message(Command("setlectureteacher"))
async def setlectureteacher_cmd(message: Message, session):
    if not _authorized(message):
        return
    parts = _assignment_args(message, "/setlectureteacher")
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer(
            "Usage: /setlectureteacher <lecture_id>|<teacher_id|none>"
        )
        return
    teacher_id, error = _teacher_id_arg(parts[1])
    if error:
        await message.answer(error)
        return
    lecture, error = await set_lecture_teacher(
        session, int(parts[0]), teacher_id
    )
    if error:
        await message.answer(error)
        return
    if teacher_id is None:
        await message.answer(f"Teacher cleared for lecture #{lecture.id}.")
        return
    teacher = await session.get(Teacher, teacher_id)
    await message.answer(
        f"Lecture #{lecture.id} teacher set to #{teacher.id} {teacher.name}."
    )


@router.message(Command("pause"))
async def pause_cmd(message: Message, session):
    if not _authorized(message):
        return
    await pause_ingestion(session)
    await message.answer("Ingestion paused. New uploads will be stored but not classified.")


@router.message(Command("resume"))
async def resume_cmd(message: Message, session):
    if not _authorized(message):
        return
    count = await resume_ingestion(session)
    await message.answer(f"Ingestion resumed. Reclassified {count} paused item(s).")


@router.message(Command("addcourse"))
async def addcourse_cmd(message: Message, session):
    if not _authorized(message):
        return
    raw = message.text.replace("/addcourse", "", 1).strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 1 or not parts[0]:
        await message.answer(
            "Usage: /addcourse <subject_slug>|<name>|[aliases]|[destination_chat]|[topic_id]\n"
            "Example: /addcourse anatomy|Anatomy|anatomy,anat|-1001234567890|12"
        )
        return

    subject_slug = parts[0]
    subject = await get_subject(session, subject_slug)
    if not subject:
        await message.answer(
            f"Subject '{subject_slug}' not found. Use /subjects to see the available subjects.\n"
            "Usage: /addcourse <subject_slug>|<name>|[aliases]|[destination_chat]|[topic_id]"
        )
        return

    name = parts[1] if len(parts) > 1 and parts[1] else subject.name
    aliases = parts[2] if len(parts) > 2 and parts[2] else subject.slug
    destination = parts[3] if len(parts) > 3 and parts[3] else ""
    topic_raw = parts[4] if len(parts) > 4 else ""

    topic_id = None
    if topic_raw:
        if not topic_raw.isdigit():
            await message.answer("Topic ID must be a number.")
            return
        topic_id = int(topic_raw)

    if not destination:
        destination = get_settings().destination_group_id or ""
    destination = destination or None

    course, created = await upsert_course(
        session,
        subject_id=subject.id,
        name=name,
        aliases=aliases,
        destination_chat_id=destination,
        topic_id=topic_id,
    )
    verb = "Added" if created else "Updated"
    topic = course.topic_id if course.topic_id is not None else "not set"
    target = course.destination_chat_id or "destination not set"
    await message.answer(
        f"{verb} course #{course.id}: {subject.name} / {course.name} -> {target} (topic {topic})"
    )


@router.message(Command("courses"))
async def courses_cmd(message: Message, session):
    if not _authorized(message):
        return
    courses = await list_courses(session)
    if not courses:
        await message.answer("No courses configured. Use /addcourse.")
        return
    lines = ["Courses:"]
    for course in courses:
        destination = course.destination_chat_id or "destination not set"
        topic = course.topic_id if course.topic_id is not None else "topic not set"
        subject = getattr(course, "subject", None)
        subject_name = subject.name if subject else "?"
        label = f"{course.year} / {course.name}" if course.year else course.name
        lines.append(
            f"#{course.id} {subject_name} / {label} "
            f"[{course.aliases or 'no aliases'}] -> {destination} (topic {topic})"
        )
    await _reply_lines(message, lines)
