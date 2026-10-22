from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from bot.config import get_settings
from bot.services.ingestion import ingest_media
from bot.utils.telegram import chat_matches

router = Router()
logger = logging.getLogger(__name__)


def _forward_ids(message: Message) -> tuple[int | None, int | None]:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None, None
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(origin, "message_id", None)
    return chat_id, message_id


@router.message(F.video | F.document)
async def ingest_message(message: Message, session):
    settings = get_settings()
    if not chat_matches(settings.upload_inbox_chat_id, message.chat):
        return

    media = message.video or message.document
    if media is None:
        return

    forward_chat_id, forward_message_id = _forward_ids(message)
    try:
        item, created = await ingest_media(
            session,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
            media_type="video" if message.video else "document",
            caption=message.caption,
            file_name=getattr(media, "file_name", None),
            file_id=getattr(media, "file_id", None),
            file_unique_id=getattr(media, "file_unique_id", None),
            forward_from_chat_id=forward_chat_id,
            forward_from_message_id=forward_message_id,
        )
    except Exception:
        logger.exception(
            "Failed to store upload chat=%s message=%s media=%s file_unique_id=%s",
            message.chat.id,
            message.message_id,
            "video" if message.video else "document",
            getattr(media, "file_unique_id", None),
        )
        raise

    if not created:
        return

    logger.info(
        "Stored upload item #%s in batch #%s status=%s reason=%s",
        item.id,
        item.batch_id,
        item.status,
        item.review_reason,
    )
