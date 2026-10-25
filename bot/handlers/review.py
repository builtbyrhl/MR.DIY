from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.ingestion_repo import (
    approve_batch,
    assign_item,
    get_batch,
    get_batch_items,
    get_course,
    list_batches,
    list_courses,
    set_item_course,
    set_item_number,
    set_item_teacher,
    set_item_topics,
    set_items_teacher_bulk,
    skip_item,
    upsert_course,
)
from bot.config import get_settings
from bot.db.models import Teacher
from bot.db.repo import (
    add_teacher,
    add_teacher_alias,
    add_topic,
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
    keep_item,
    pause_ingestion,
    resume_ingestion,
    retry_item,
    set_item_caption,
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
            await message.answer(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await message.answer(chunk)


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
