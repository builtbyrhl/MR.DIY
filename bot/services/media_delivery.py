from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_settings
from bot.db.models import StudentMediaMessage
from bot.db.session import get_session_factory

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_DELETING = "deleting"
STATUS_DELETED = "deleted"
STATUS_FAILED = "failed"
STATUS_MISSED = "missed"

MIN_RETENTION_HOURS = 1
MAX_RETENTION_HOURS = 48
MAX_TELEGRAM_DELETE_AGE = timedelta(hours=48)
RETENTION_SAFETY_MARGIN = timedelta(minutes=5)
MAX_DELETE_ATTEMPTS = 5
STALE_CLAIM_AGE = timedelta(minutes=5)
DEFAULT_BATCH_SIZE = 50


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def retention_hours(value: Optional[int] = None) -> int:
    raw = get_settings().media_retention_hours if value is None else value
    return min(max(int(raw), MIN_RETENTION_HOURS), MAX_RETENTION_HOURS)


def deletion_deadline(
    sent_at: datetime, hours: Optional[int] = None
) -> datetime:
    effective_hours = retention_hours(hours)
    deadline = _as_naive_utc(sent_at) + timedelta(
        hours=effective_hours
    )
    if effective_hours >= MAX_RETENTION_HOURS:
        deadline -= RETENTION_SAFETY_MARGIN
    return deadline


async def record_delivered_media(
    session: AsyncSession,
    *,
    user_id: int,
    lecture_id: Optional[int],
    chat_id: int,
    message_id: int,
    sent_at: Optional[datetime] = None,
    retention: Optional[int] = None,
) -> Optional[StudentMediaMessage]:
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None
    if message_id <= 0:
        return None

    sent = _as_naive_utc(sent_at) if sent_at is not None else utcnow()
    record = StudentMediaMessage(
        user_id=user_id,
        lecture_id=lecture_id,
        chat_id=chat_id,
        message_id=message_id,
        sent_at=sent,
        delete_at=deletion_deadline(sent, retention),
        status=STATUS_PENDING,
        attempts=0,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await session.scalar(
            select(StudentMediaMessage).where(
                StudentMediaMessage.chat_id == chat_id,
                StudentMediaMessage.message_id == message_id,
            )
        )
    await session.refresh(record)
    return record


async def reset_stale_claims(
    session: AsyncSession, *, now: Optional[datetime] = None
) -> int:
    now = now or utcnow()
    result = await session.execute(
        update(StudentMediaMessage)
        .where(
            StudentMediaMessage.status == STATUS_DELETING,
            StudentMediaMessage.claimed_at.is_not(None),
            StudentMediaMessage.claimed_at <= now - STALE_CLAIM_AGE,
        )
        .values(status=STATUS_PENDING, claimed_at=None)
    )
    await session.commit()
    return result.rowcount or 0


async def _claim_due_messages(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = DEFAULT_BATCH_SIZE,
) -> list[StudentMediaMessage]:
    candidate_ids = [
        record.id
        for record in (
            await session.scalars(
                select(StudentMediaMessage)
                .where(
                    StudentMediaMessage.status == STATUS_PENDING,
                    StudentMediaMessage.delete_at <= now,
                )
                .order_by(
                    StudentMediaMessage.delete_at,
                    StudentMediaMessage.id,
                )
                .limit(limit)
            )
        ).all()
    ]

    claimed = []
    for candidate_id in candidate_ids:
        result = await session.execute(
            update(StudentMediaMessage)
            .where(
                StudentMediaMessage.id == candidate_id,
                StudentMediaMessage.status == STATUS_PENDING,
            )
            .values(status=STATUS_DELETING, claimed_at=now)
        )
        await session.commit()
        if result.rowcount == 1:
            record = await session.get(StudentMediaMessage, candidate_id)
            if record is not None:
                claimed.append(record)
    return claimed


def _mark_retry_or_failure(
    record: StudentMediaMessage, error: str
) -> None:
    record.attempts = (record.attempts or 0) + 1
    record.claimed_at = None
    record.last_error = error
    record.status = (
        STATUS_FAILED
        if record.attempts >= MAX_DELETE_ATTEMPTS
        else STATUS_PENDING
    )


async def _delete_one(
    bot: Bot,
    session: AsyncSession,
    record: StudentMediaMessage,
    *,
    now: datetime,
) -> bool:
    sent_at = (
        _as_naive_utc(record.sent_at)
        if record.sent_at is not None
        else None
    )
    if (
        sent_at is not None
        and now - sent_at >= MAX_TELEGRAM_DELETE_AGE
    ):
        record.status = STATUS_MISSED
        record.claimed_at = None
        record.last_error = (
            "Telegram's 48-hour deletion window passed before cleanup."
        )
        await session.commit()
        return False

    try:
        await bot.delete_message(
            chat_id=record.chat_id,
            message_id=record.message_id,
        )
    except TelegramRetryAfter as exc:
        record.attempts = (record.attempts or 0) + 1
        record.claimed_at = None
        record.last_error = (
            f"Telegram flood control asked to retry after "
            f"{exc.retry_after} seconds."
        )
        record.delete_at = now + timedelta(
            seconds=max(int(exc.retry_after), 1)
        )
        record.status = STATUS_PENDING
        await session.commit()
        return False
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound) as exc:
        detail = str(exc)
        lowered = detail.lower()
        if (
            "not found" in lowered
            or "can't be deleted" in lowered
            or "too old" in lowered
            or "message to delete" in lowered
        ):
            record.status = STATUS_MISSED
            record.claimed_at = None
            record.last_error = detail
        else:
            _mark_retry_or_failure(record, detail)
        await session.commit()
        return False
    except Exception as exc:
        logger.warning(
            "Could not delete student media message %s/%s",
            record.chat_id,
            record.message_id,
            exc_info=True,
        )
        _mark_retry_or_failure(record, str(exc))
        await session.commit()
        return False

    record.status = STATUS_DELETED
    record.deleted_at = now
    record.claimed_at = None
    record.last_error = None
    await session.commit()
    return True


async def process_due_deletions(
    bot: Bot,
    *,
    session_factory=None,
    now: Optional[datetime] = None,
    limit: int = DEFAULT_BATCH_SIZE,
) -> int:
    now = _as_naive_utc(now) if now is not None else utcnow()
    factory = session_factory or get_session_factory()
    deleted = 0
    async with factory() as session:
        await reset_stale_claims(session, now=now)
        records = await _claim_due_messages(
            session, now=now, limit=limit
        )
        for record in records:
            try:
                if await _delete_one(bot, session, record, now=now):
                    deleted += 1
            except Exception:
                await session.rollback()
                logger.exception(
                    "Unexpected media cleanup failure for record #%s",
                    record.id,
                )
    return deleted


async def run_media_cleanup(bot: Bot) -> None:
    interval = max(
        int(get_settings().media_cleanup_interval_seconds), 30
    )
    while True:
        try:
            await process_due_deletions(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Media cleanup cycle failed")
        await asyncio.sleep(interval)


def start_media_cleanup(bot: Bot) -> asyncio.Task:
    task = asyncio.create_task(
        run_media_cleanup(bot), name="student-media-cleanup"
    )
    logger.info(
        "Student media cleanup started (retention=%s hours, interval=%s seconds)",
        retention_hours(),
        get_settings().media_cleanup_interval_seconds,
    )
    return task
