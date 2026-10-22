from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.ingestion_repo import (
    approve_batch,
    get_batch,
    get_batch_items,
    get_course,
    list_batches,
    list_courses,
    set_item_course,
    set_item_number,
    skip_item,
    upsert_course,
)
from bot.config import get_settings
from bot.db.repo import get_subject
from bot.errors import ApprovedBatchError
from bot.services.auth import require_admin
from bot.services.ingestion import (
    keep_item,
    pause_ingestion,
    resume_ingestion,
    retry_item,
)
from bot.services.publishing import publish_batch

router = Router()


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

def _args(message: Message, command: str) -> list[str]:
    return message.text.replace(command, "", 1).strip().split()


def _item_line(item) -> str:
    number = f"L{item.lecture_number}" if item.lecture_number is not None else "L?"
    flags = []
    if item.duplicate_flag:
        flags.append(f"dup of #{item.duplicate_of_item_id}")
    if item.review_reason:
        flags.append(item.review_reason)
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
