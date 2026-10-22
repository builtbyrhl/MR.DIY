from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.ingestion_repo import get_batch, get_batch_items
from bot.db.models import Course, UploadBatch, UploadItem

CAPTION_LIMIT = 1024

PUBLISHED = "published"
ALREADY_PUBLISHED = "already_published"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(frozen=True)
class PublishResult:
    item_id: int
    status: str
    message_id: Optional[int] = None
    reason: Optional[str] = None


@dataclass
class BatchPublishResult:
    batch_id: int
    results: list[PublishResult] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def published(self) -> int:
        return sum(result.status == PUBLISHED for result in self.results)

    @property
    def already_published(self) -> int:
        return sum(result.status == ALREADY_PUBLISHED for result in self.results)

    @property
    def skipped(self) -> int:
        return sum(result.status == SKIPPED for result in self.results)

    @property
    def failures(self) -> list[PublishResult]:
        return [result for result in self.results if result.status == FAILED]


def _normalise_destination(raw: str) -> int | str:
    value = raw.strip()
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    return value


def _resolve_destination(course: Course) -> Optional[int | str]:
    raw = (course.destination_chat_id or "").strip()
    if not raw:
        raw = (get_settings().destination_group_id or "").strip()
    if not raw:
        return None
    return _normalise_destination(raw)


def _build_caption(item: UploadItem) -> Optional[str]:
    text = (item.title or item.raw_caption or "").strip()
    if not text:
        return None
    if len(text) > CAPTION_LIMIT:
        return text[: CAPTION_LIMIT - 3].rstrip() + "..."
    return text


async def _mark_failed(
    session: AsyncSession, item: UploadItem, reason: str
) -> PublishResult:
    item.publication_status = FAILED
    item.error = reason
    await session.commit()
    return PublishResult(item_id=item.id, status=FAILED, reason=reason)


async def _publish_item(
    session: AsyncSession,
    bot: Bot,
    item: UploadItem,
) -> PublishResult:
    if item.status == "skipped":
        return PublishResult(
            item_id=item.id,
            status=SKIPPED,
            reason="item is marked skipped",
        )

    if item.publication_status == PUBLISHED or item.published_message_id is not None:
        return PublishResult(
            item_id=item.id,
            status=ALREADY_PUBLISHED,
            message_id=item.published_message_id,
        )

    if not item.file_id:
        return await _mark_failed(
            session,
            item,
            "missing Telegram file_id; re-forward the upload so it can be stored",
        )

    if item.media_type not in {"video", "document"}:
        return await _mark_failed(
            session,
            item,
            f"unsupported media type: {item.media_type}",
        )

    if item.course_id is None:
        return await _mark_failed(
            session,
            item,
            "no course assigned to this item",
        )

    course = await session.get(Course, item.course_id)
    if course is None:
        return await _mark_failed(
            session,
            item,
            f"course #{item.course_id} no longer exists",
        )

    destination = _resolve_destination(course)
    if destination is None:
        return await _mark_failed(
            session,
            item,
            f"destination chat is not configured for course #{course.id} "
            "and DESTINATION_GROUP_ID is not set",
        )

    if course.topic_id is None:
        return await _mark_failed(
            session,
            item,
            f"topic id is not configured for course #{course.id}",
        )

    caption = _build_caption(item)
    try:
        if item.media_type == "video":
            sent = await bot.send_video(
                chat_id=destination,
                video=item.file_id,
                caption=caption,
                parse_mode=None,
                message_thread_id=course.topic_id,
            )
        else:
            sent = await bot.send_document(
                chat_id=destination,
                document=item.file_id,
                caption=caption,
                parse_mode=None,
                message_thread_id=course.topic_id,
            )
    except Exception as exc:
        return await _mark_failed(
            session,
            item,
            f"Telegram send failed: {exc}",
        )

    message_id = getattr(sent, "message_id", None)
    if message_id is None:
        return await _mark_failed(
            session,
            item,
            "Telegram response did not include a message id",
        )

    item.publication_status = PUBLISHED
    item.published_message_id = message_id
    item.error = None
    await session.commit()
    return PublishResult(
        item_id=item.id,
        status=PUBLISHED,
        message_id=message_id,
    )


async def publish_item(
    session: AsyncSession, bot: Bot, item_id: int
) -> PublishResult:
    item = await session.get(UploadItem, item_id)
    if item is None:
        return PublishResult(
            item_id=item_id,
            status=FAILED,
            reason="Item not found.",
        )

    batch = await session.get(UploadBatch, item.batch_id)
    if batch is None:
        return PublishResult(
            item_id=item.id,
            status=FAILED,
            reason=f"Batch #{item.batch_id} not found.",
        )
    if batch.status != "approved":
        return PublishResult(
            item_id=item.id,
            status=FAILED,
            reason=f"Batch #{batch.id} is not approved. Use /approve first.",
        )

    return await _publish_item(session, bot, item)


async def publish_batch(
    session: AsyncSession, bot: Bot, batch_id: int
) -> BatchPublishResult:
    batch = await get_batch(session, batch_id)
    if batch is None:
        return BatchPublishResult(batch_id=batch_id, error="Batch not found.")
    if batch.status != "approved":
        return BatchPublishResult(
            batch_id=batch_id,
            error=f"Batch #{batch.id} is not approved. Use /approve first.",
        )

    result = BatchPublishResult(batch_id=batch_id)
    items = await get_batch_items(session, batch_id)
    for item in items:
        result.results.append(await _publish_item(session, bot, item))
    return result
