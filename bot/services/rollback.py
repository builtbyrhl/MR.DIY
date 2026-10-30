from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select, update

from bot.config import get_settings
from bot.db.models import (
    Course,
    Lecture,
    RollbackItem,
    RollbackOperation,
    UploadBatch,
    UploadItem,
)
from bot.db.repo import record_admin_action
from bot.services.publishing import (
    PUBLISHED,
    WITHDRAWN,
    _normalise_destination,
    _resolve_destination,
)

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
PARTIAL = "partial"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

CHANNEL_DELETED = "deleted"
CHANNEL_ALREADY_DELETED = "already_deleted"
CHANNEL_NOT_LINKED = "not_linked"
CHANNEL_FAILED = "failed"

CATALOGUE_WITHDRAWN = "withdrawn"
CATALOGUE_ALREADY_WITHDRAWN = "already_withdrawn"
CATALOGUE_FAILED = "failed"

_CHANNEL_CONFIRMED = {CHANNEL_DELETED, CHANNEL_ALREADY_DELETED}
_CHANNEL_DONE = _CHANNEL_CONFIRMED | {CHANNEL_NOT_LINKED}
_CATALOGUE_DONE = {CATALOGUE_WITHDRAWN, CATALOGUE_ALREADY_WITHDRAWN}


@dataclass
class RollbackPreview:
    operation: RollbackOperation
    items: list[RollbackItem]
    skipped_withdrawn: int = 0
    skipped_unprojected: int = 0


async def _target_chat(session, item: Optional[UploadItem]) -> Optional[str]:
    if item is None:
        return None
    if item.published_chat_id:
        return str(_normalise_destination(item.published_chat_id))
    if item.course_id is not None:
        course = await session.get(Course, item.course_id)
        if course is not None:
            destination = _resolve_destination(course)
            if destination is not None:
                return str(destination)
    raw = (get_settings().destination_group_id or "").strip()
    if not raw:
        return None
    return str(_normalise_destination(raw))


def _is_withdrawn(lecture: Lecture) -> bool:
    return bool(lecture.is_withdrawn)


async def _has_unfinished_rollback(session, lecture_id: int) -> bool:
    res = await session.execute(
        select(RollbackItem)
        .join(RollbackOperation, RollbackOperation.id == RollbackItem.operation_id)
        .where(
            RollbackItem.lecture_id == lecture_id,
            RollbackOperation.status.in_([PENDING, RUNNING, PARTIAL, FAILED]),
        )
        .order_by(RollbackItem.id.desc())
        .limit(1)
    )
    item = res.scalar_one_or_none()
    if item is None:
        return False
    return (
        item.channel_status not in _CHANNEL_DONE
        or item.catalogue_status not in _CATALOGUE_DONE
    )


async def _lectures_for_batch(session, batch_id: int) -> list[Lecture]:
    upload_ids = select(UploadItem.id).where(UploadItem.batch_id == batch_id)
    res = await session.execute(
        select(Lecture)
        .where(Lecture.source_upload_item_id.in_(upload_ids))
        .order_by(Lecture.id)
    )
    return list(res.scalars().all())


async def create_preview(
    session,
    admin_id: int,
    *,
    lecture_id: Optional[int] = None,
    batch_id: Optional[int] = None,
) -> tuple[Optional[RollbackPreview], Optional[str]]:
    if (lecture_id is None) == (batch_id is None):
        return None, "Choose exactly one lecture or batch."

    lectures: list[Lecture]
    if lecture_id is not None:
        lecture = await session.get(Lecture, lecture_id)
        if lecture is None:
            return None, "Lecture not found."
        lectures = [lecture]
    else:
        batch = await session.get(UploadBatch, batch_id)
        if batch is None:
            return None, "Batch not found."
        lectures = await _lectures_for_batch(session, batch_id)

    actionable: list[Lecture] = []
    skipped_withdrawn = 0
    for lecture in lectures:
        if not _is_withdrawn(lecture):
            actionable.append(lecture)
        elif await _has_unfinished_rollback(session, lecture.id):
            actionable.append(lecture)
        else:
            skipped_withdrawn += 1

    item_ids = [
        lecture.source_upload_item_id
        for lecture in actionable
        if lecture.source_upload_item_id is not None
    ]
    upload_items: dict[int, UploadItem] = {}
    if item_ids:
        res = await session.execute(
            select(UploadItem).where(UploadItem.id.in_(item_ids))
        )
        upload_items = {item.id: item for item in res.scalars().all()}

    projected = [
        lecture
        for lecture in actionable
        if lecture.source_upload_item_id in upload_items
        and (
            upload_items[lecture.source_upload_item_id].publication_status == PUBLISHED
            or upload_items[lecture.source_upload_item_id].published_message_id is not None
        )
    ]
    skipped_unprojected = len(actionable) - len(projected)

    if not projected:
        if skipped_withdrawn:
            return None, "All matching lectures are already withdrawn."
        return None, "No published lectures found for this target."

    target_type = "lecture" if lecture_id is not None else "batch"
    target_id = lecture_id if lecture_id is not None else batch_id
    operation = RollbackOperation(
        admin_id=admin_id,
        target_type=target_type,
        target_id=target_id,
        source_batch_id=batch_id,
        status=PENDING,
        total_count=len(projected),
    )
    session.add(operation)
    await session.flush()

    rollback_items: list[RollbackItem] = []
    for lecture in projected:
        upload_item = upload_items[lecture.source_upload_item_id]
        rollback_item = RollbackItem(
            operation_id=operation.id,
            lecture_id=lecture.id,
            upload_item_id=upload_item.id,
            target_chat_id=await _target_chat(session, upload_item),
            published_message_id=upload_item.published_message_id,
            channel_status=PENDING,
            catalogue_status=PENDING,
        )
        session.add(rollback_item)
        rollback_items.append(rollback_item)
    await session.commit()
    await session.refresh(operation)
    return (
        RollbackPreview(
            operation=operation,
            items=rollback_items,
            skipped_withdrawn=skipped_withdrawn,
            skipped_unprojected=skipped_unprojected,
        ),
        None,
    )


async def get_operation(session, operation_id: int) -> Optional[RollbackOperation]:
    return await session.get(RollbackOperation, operation_id)


async def get_operation_items(session, operation_id: int) -> list[RollbackItem]:
    res = await session.execute(
        select(RollbackItem)
        .where(RollbackItem.operation_id == operation_id)
        .order_by(RollbackItem.id)
    )
    return list(res.scalars().all())


async def cancel_operation(session, operation_id: int, admin_id: int) -> bool:
    operation = await session.get(RollbackOperation, operation_id)
    if operation is None:
        return False
    result = await session.execute(
        update(RollbackOperation)
        .where(
            RollbackOperation.id == operation_id,
            RollbackOperation.status == PENDING,
        )
        .values(status=CANCELLED, completed_at=datetime.utcnow())
    )
    if result.rowcount != 1:
        await session.rollback()
        return False
    await session.commit()
    await record_admin_action(
        session,
        admin_id,
        "cancel_unpublish",
        target_type=operation.target_type,
        target_id=operation.target_id,
        details=f"operation={operation.id}",
    )
    return True


def _message_already_gone(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return (
        "message to delete not found" in text
        or "message not found" in text
        or "message_id_invalid" in text
    )


async def _delete_channel_message(bot: Bot, item: RollbackItem) -> tuple[str, Optional[str]]:
    if not item.target_chat_id or item.published_message_id is None:
        return CHANNEL_NOT_LINKED, "No stable target message was recorded."
    try:
        result = await bot.delete_message(
            chat_id=item.target_chat_id,
            message_id=item.published_message_id,
        )
    except TelegramBadRequest as exc:
        if _message_already_gone(exc):
            return CHANNEL_ALREADY_DELETED, None
        return CHANNEL_FAILED, str(exc)[:500]
    except TelegramForbiddenError as exc:
        return CHANNEL_FAILED, f"Forbidden: {exc}"[:500]
    except TelegramAPIError as exc:
        return CHANNEL_FAILED, str(exc)[:500]
    except Exception as exc:
        logger.exception("Unexpected delete error for rollback item %s", item.id)
        return CHANNEL_FAILED, str(exc)[:500]
    if result is False:
        return CHANNEL_FAILED, "Telegram did not confirm the deletion."
    return CHANNEL_DELETED, None


async def _withdraw_catalogue(session, rollback_item: RollbackItem) -> tuple[str, Optional[str]]:
    lecture = await session.get(Lecture, rollback_item.lecture_id)
    if lecture is None:
        return CATALOGUE_FAILED, "Lecture record no longer exists."
    try:
        upload_item = None
        if rollback_item.upload_item_id is not None:
            upload_item = await session.get(UploadItem, rollback_item.upload_item_id)
        if lecture.is_withdrawn:
            if upload_item is not None:
                upload_item.publication_status = WITHDRAWN
            await session.commit()
            return CATALOGUE_ALREADY_WITHDRAWN, None
        lecture.is_withdrawn = True
        lecture.withdrawal_status = PARTIAL
        lecture.withdrawn_at = datetime.utcnow()
        if upload_item is not None:
            upload_item.publication_status = WITHDRAWN
        await session.commit()
        return CATALOGUE_WITHDRAWN, None
    except Exception as exc:
        await session.rollback()
        logger.exception("Catalogue withdrawal failed for lecture %s", rollback_item.lecture_id)
        return CATALOGUE_FAILED, str(exc)[:500]


async def _execute_item(session, bot: Bot, item: RollbackItem) -> None:
    if item.channel_status not in _CHANNEL_DONE:
        item.attempts = (item.attempts or 0) + 1
        status, error = await _delete_channel_message(bot, item)
        item.channel_status = status
        item.channel_error = error
        await session.commit()

    if item.catalogue_status not in _CATALOGUE_DONE:
        status, error = await _withdraw_catalogue(session, item)
        item.catalogue_status = status
        item.catalogue_error = error
        await session.commit()

    lecture = await session.get(Lecture, item.lecture_id)
    if lecture is None:
        return
    channel_ok = item.channel_status in _CHANNEL_CONFIRMED
    catalogue_ok = item.catalogue_status in _CATALOGUE_DONE
    if catalogue_ok and not lecture.is_withdrawn:
        lecture.is_withdrawn = True
        lecture.withdrawn_at = lecture.withdrawn_at or datetime.utcnow()
    if channel_ok and catalogue_ok:
        lecture.withdrawal_status = "withdrawn"
    elif item.channel_status in _CHANNEL_DONE or catalogue_ok:
        lecture.withdrawal_status = PARTIAL
    else:
        lecture.withdrawal_status = PENDING
    await session.commit()


def _operation_status(items: list[RollbackItem]) -> str:
    if not items:
        return FAILED
    complete = [
        item.channel_status in _CHANNEL_CONFIRMED
        and item.catalogue_status in _CATALOGUE_DONE
        for item in items
    ]
    if all(complete):
        return COMPLETED
    progressed = [
        item.channel_status in _CHANNEL_DONE
        or item.catalogue_status in _CATALOGUE_DONE
        for item in items
    ]
    if not any(progressed):
        return FAILED
    return PARTIAL


async def execute_operation(
    session, bot: Bot, operation_id: int, admin_id: int
) -> tuple[Optional[RollbackOperation], Optional[str]]:
    operation = await session.get(RollbackOperation, operation_id)
    if operation is None:
        return None, "Unpublish preview not found."

    result = await session.execute(
        update(RollbackOperation)
        .where(
            RollbackOperation.id == operation_id,
            RollbackOperation.status.notin_([RUNNING, COMPLETED, CANCELLED]),
        )
        .values(
            status=RUNNING,
            started_at=datetime.utcnow(),
            completed_at=None,
            error=None,
        )
    )
    if result.rowcount != 1:
        await session.rollback()
        operation = await session.get(RollbackOperation, operation_id)
        if operation is None:
            return None, "Unpublish preview not found."
        if operation.status == RUNNING:
            return operation, "This unpublish operation is already running."
        return operation, "This unpublish operation is no longer executable."
    await session.commit()
    await session.refresh(operation)

    items = await get_operation_items(session, operation_id)
    try:
        for item in items:
            await _execute_item(session, bot, item)
    except Exception as exc:
        logger.exception("Unpublish operation %s failed", operation_id)
        await session.rollback()
        operation = await session.get(RollbackOperation, operation_id)
        if operation is not None:
            operation.error = str(exc)[:1000]
            operation.status = PARTIAL
            await session.commit()
        return operation, "Unpublish stopped after an unexpected error."

    items = await get_operation_items(session, operation_id)
    operation.status = _operation_status(items)
    operation.completed_at = datetime.utcnow()
    await session.commit()
    await record_admin_action(
        session,
        admin_id,
        "unpublish",
        target_type=operation.target_type,
        target_id=operation.target_id,
        details=f"operation={operation.id}; status={operation.status}; items={len(items)}",
    )
    return operation, None
