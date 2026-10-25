from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.ingestion_repo import get_batch, get_batch_items
from bot.db.models import Course, UploadBatch, UploadItem
from bot.services.catalog import project_upload_to_lecture

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024

PUBLISHED = "published"
PUBLISHING = "publishing"
ALREADY_PUBLISHED = "already_published"
SKIPPED = "skipped"
FAILED = "failed"

UNCERTAIN_REASON = (
    "a previous publish attempt did not complete. If the message exists in "
    "the destination topic, use /reconcilepublish; otherwise use "
    "/unlockpublish before retrying."
)

DEFINITE_SEND_ERRORS = (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramUnauthorizedError,
)


def _uncertain_send_reason(detail: str) -> str:
    return (
        f"Telegram send outcome is uncertain: {detail}. If the message exists "
        "in the destination topic, use /reconcilepublish; otherwise use "
        "/unlockpublish before retrying."
    )


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


async def _mark_uncertain(
    session: AsyncSession, item: UploadItem, reason: str
) -> PublishResult:
    """Keep the publishing claim when Telegram's outcome cannot be known.

    A retry in this state could create a duplicate, so an administrator must
    inspect the destination topic and explicitly unlock the item.
    """
    item.publication_status = PUBLISHING
    item.error = reason
    await session.commit()
    return PublishResult(item_id=item.id, status=FAILED, reason=reason)


async def _claim_for_publish(
    session: AsyncSession, item: UploadItem
) -> bool:
    """Atomically move an unpublished item into the publishing state.

    The conditional UPDATE prevents two concurrent /publish runs from sending
    the same item. Items left in this state by a crash are never auto-resent.
    """
    result = await session.execute(
        update(UploadItem)
        .where(
            UploadItem.id == item.id,
            UploadItem.published_message_id.is_(None),
            or_(
                UploadItem.publication_status.is_(None),
                UploadItem.publication_status.notin_((PUBLISHED, PUBLISHING)),
            ),
        )
        .values(publication_status=PUBLISHING, error=None)
    )
    await session.commit()
    await session.refresh(item)
    return result.rowcount == 1


async def _sync_student_catalog(
    session: AsyncSession, item: UploadItem
) -> None:
    """Best-effort projection; sending already succeeded at this point."""
    item_id = item.id
    try:
        await project_upload_to_lecture(session, item)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Failed to project published upload #%s", item_id)


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
        await _sync_student_catalog(session, item)
        return PublishResult(
            item_id=item.id,
            status=ALREADY_PUBLISHED,
            message_id=item.published_message_id,
        )

    if item.publication_status == PUBLISHING:
        return PublishResult(
            item_id=item.id,
            status=FAILED,
            reason=UNCERTAIN_REASON,
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

    if not await _claim_for_publish(session, item):
        if item.publication_status == PUBLISHED or item.published_message_id is not None:
            await _sync_student_catalog(session, item)
            return PublishResult(
                item_id=item.id,
                status=ALREADY_PUBLISHED,
                message_id=item.published_message_id,
            )
        if item.publication_status == PUBLISHING:
            return PublishResult(
                item_id=item.id,
                status=FAILED,
                reason=UNCERTAIN_REASON,
            )
        return PublishResult(
            item_id=item.id,
            status=FAILED,
            reason="item could not be claimed for publishing; refresh and retry.",
        )

    caption = _build_caption(item)
    try:
        if item.media_type == "video":
            sent = await bot.send_video(
                chat_id=destination,
                video=item.file_id,
                caption=caption,
                parse_mode=None,
                protect_content=True,
                message_thread_id=course.topic_id,
            )
        else:
            sent = await bot.send_document(
                chat_id=destination,
                document=item.file_id,
                caption=caption,
                parse_mode=None,
                protect_content=True,
                message_thread_id=course.topic_id,
            )
    except TelegramRetryAfter as exc:
        return await _mark_failed(
            session,
            item,
            "Telegram flood control rejected the send; no message was sent. "
            f"Retry after {exc.retry_after} seconds.",
        )
    except Exception as exc:
        if isinstance(exc, DEFINITE_SEND_ERRORS):
            return await _mark_failed(
                session,
                item,
                f"Telegram rejected the send: {exc}",
            )
        return await _mark_uncertain(
            session,
            item,
            _uncertain_send_reason(str(exc)),
        )

    message_id = getattr(sent, "message_id", None)
    if message_id is None:
        return await _mark_uncertain(
            session,
            item,
            _uncertain_send_reason(
                "Telegram response did not include a message id"
            ),
        )

    item.publication_status = PUBLISHED
    item.published_message_id = message_id
    item.error = None
    await session.commit()
    await _sync_student_catalog(session, item)
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


async def unlock_publish_state(
    session: AsyncSession, item_id: int
) -> tuple[Optional[UploadItem], Optional[str]]:
    """Clear a stale publishing claim after an admin has checked the topic."""
    item = await session.get(UploadItem, item_id)
    if item is None:
        return None, "Item not found."
    if item.publication_status != PUBLISHING:
        return None, "Item is not in an uncertain publishing state."
    item.publication_status = None
    item.error = None
    await session.commit()
    await session.refresh(item)
    return item, None


async def reconcile_publish_state(
    session: AsyncSession,
    item_id: int,
    *,
    message_id: int,
    destination_chat_id: int | str,
    topic_id: int,
) -> tuple[Optional[UploadItem], Optional[str]]:
    """Record an already-sent message without sending it again.

    This is intentionally narrower than /unlockpublish: it only accepts a
    claimed item whose approved course matches the supplied destination and
    topic. The conditional UPDATE keeps a concurrent state change from being
    silently overwritten.
    """
    if message_id <= 0:
        return None, "Message ID must be a positive integer."

    raw_destination = str(destination_chat_id).strip()
    if not raw_destination:
        return None, "Destination chat ID is required."
    destination = _normalise_destination(raw_destination)

    if topic_id <= 0:
        return None, "Topic ID must be a positive integer."

    item = await session.get(UploadItem, item_id)
    if item is None:
        return None, "Item not found."
    if item.status == "skipped":
        return None, "Item is skipped; use /keep before reconciling it."
    if item.publication_status != PUBLISHING or item.published_message_id is not None:
        return None, "Item is not in an uncertain publishing state."

    batch = await session.get(UploadBatch, item.batch_id)
    if batch is None or batch.status != "approved":
        return None, "The item's batch is not approved."

    if item.course_id is None:
        return None, "No course is assigned to this item."
    course = await session.get(Course, item.course_id)
    if course is None:
        return None, f"Course #{item.course_id} no longer exists."

    configured_destination = _resolve_destination(course)
    if configured_destination is None:
        return None, "Destination chat is not configured for this course."
    if destination != configured_destination:
        return None, (
            "Destination chat does not match this item's course "
            f"({configured_destination})."
        )

    if course.topic_id is None:
        return None, "Topic ID is not configured for this course."
    if topic_id != course.topic_id:
        return None, (
            "Topic ID does not match this item's course "
            f"({course.topic_id})."
        )

    try:
        result = await session.execute(
            update(UploadItem)
            .where(
                UploadItem.id == item.id,
                UploadItem.publication_status == PUBLISHING,
                UploadItem.published_message_id.is_(None),
            )
            .values(
                publication_status=PUBLISHED,
                published_message_id=message_id,
                error=None,
            )
        )
        if result.rowcount != 1:
            await session.rollback()
            return None, (
                "Item state changed while reconciling; refresh and verify "
                "the destination before retrying."
            )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Failed to reconcile publish state for upload #%s", item_id)
        return None, "Could not reconcile publish state; no changes were applied."

    await session.refresh(item)
    await _sync_student_catalog(session, item)
    await session.refresh(item)
    return item, None


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
