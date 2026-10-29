from __future__ import annotations

import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message, ReplyParameters

from bot.config import get_settings
from bot.db.ingestion_repo import get_item_by_source
from bot.services.auth import is_admin
from bot.services.ingestion import ingest_media, update_ingested_caption
from bot.utils.telegram import chat_matches

router = Router()
logger = logging.getLogger(__name__)

VIDEO_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ts",
        ".webm",
    }
)


def _forward_ids(message: Message) -> tuple[int | None, int | None]:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None, None
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(origin, "message_id", None)
    return chat_id, message_id


def _media_of(message: Message):
    return getattr(message, "video", None) or getattr(
        message, "document", None
    )


def _media_type(message: Message) -> str:
    return "video" if getattr(message, "video", None) is not None else "document"


def _is_video_document(document) -> bool:
    mime_type = (getattr(document, "mime_type", None) or "").strip().casefold()
    if mime_type.startswith("video/"):
        return True
    file_name = (getattr(document, "file_name", None) or "").strip()
    if not file_name:
        return False
    extension = os.path.splitext(file_name)[1].casefold()
    return extension in VIDEO_DOCUMENT_EXTENSIONS


def _is_video_message(message) -> bool:
    if getattr(message, "video", None) is not None:
        return True
    document = getattr(message, "document", None)
    return document is not None and _is_video_document(document)


def _upload_identifiers_ok(chat_id, message_id, media) -> bool:
    if not isinstance(chat_id, int) or chat_id == 0:
        return False
    if not isinstance(message_id, int) or message_id <= 0:
        return False
    if media is None:
        return False
    file_id = getattr(media, "file_id", None)
    file_unique_id = getattr(media, "file_unique_id", None)
    return (
        isinstance(file_id, str)
        and bool(file_id.strip())
        and isinstance(file_unique_id, str)
        and bool(file_unique_id.strip())
    )


async def _answer_videoid(message: Message, text: str) -> None:
    await message.answer(
        text,
        parse_mode=None,
        reply_parameters=ReplyParameters(
            message_id=message.message_id,
            allow_sending_without_reply=True,
        ),
    )


async def _safe_rollback(session) -> None:
    try:
        await session.rollback()
    except Exception:
        logger.exception("Could not roll back failed video ID lookup")


@router.message(F.video | F.document)
async def ingest_message(message: Message, session):
    settings = get_settings()
    if not chat_matches(settings.upload_inbox_chat_id, message.chat):
        return

    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    media = _media_of(message)
    if not _upload_identifiers_ok(chat_id, message_id, media):
        logger.warning(
            "Ignored malformed upload chat=%s message=%s media=%s",
            chat_id,
            message_id,
            _media_type(message),
        )
        return

    forward_chat_id, forward_message_id = _forward_ids(message)
    try:
        item, created = await ingest_media(
            session,
            source_chat_id=chat_id,
            source_message_id=message_id,
            media_type=_media_type(message),
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
            chat_id,
            message_id,
            _media_type(message),
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


@router.edited_message(F.video | F.document)
async def ingest_edited_message(message: Message, session):
    settings = get_settings()
    if not chat_matches(settings.upload_inbox_chat_id, message.chat):
        return

    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    media = _media_of(message)
    if not _upload_identifiers_ok(chat_id, message_id, media):
        logger.warning(
            "Ignored malformed upload edit chat=%s message=%s media=%s",
            chat_id,
            message_id,
            _media_type(message),
        )
        return

    try:
        item, outcome = await update_ingested_caption(
            session,
            source_chat_id=chat_id,
            source_message_id=message_id,
            caption=message.caption,
        )
    except Exception:
        logger.exception(
            "Failed to update edited upload chat=%s message=%s media=%s",
            chat_id,
            message_id,
            _media_type(message),
        )
        raise

    if item is None:
        logger.info(
            "Ignored edited upload chat=%s message=%s: %s",
            chat_id,
            message_id,
            outcome,
        )
        return

    logger.info(
        "Edited upload item #%s outcome=%s status=%s reason=%s",
        item.id,
        outcome,
        item.status,
        item.review_reason,
    )


@router.message(Command("videoid"))
async def videoid_cmd(message: Message, session):
    sender = getattr(message, "from_user", None)
    if sender is None or not is_admin(sender.id):
        logger.warning(
            "Ignored /videoid: sender is not an admin (chat=%s sender=%s)",
            getattr(getattr(message, "chat", None), "id", None),
            getattr(sender, "id", None),
        )
        return

    settings = get_settings()
    if not chat_matches(
        settings.upload_inbox_chat_id, getattr(message, "chat", None)
    ):
        await _answer_videoid(
            message,
            "Use /videoid in the configured inventory group, "
            "replying to a video.",
        )
        return

    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        await _answer_videoid(
            message, "Reply to a video with /videoid to see its ID."
        )
        return

    if not _is_video_message(reply):
        await _answer_videoid(
            message,
            "That message is not a video. Reply to a video sent to the "
            "inventory group.",
        )
        return

    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(reply, "message_id", None)
    try:
        item = await get_item_by_source(session, chat_id, message_id)
    except Exception:
        logger.exception(
            "Failed to look up upload item chat=%s message=%s",
            chat_id,
            message_id,
        )
        await _safe_rollback(session)
        await _answer_videoid(
            message,
            "Could not look up the video ID right now. Please try again later.",
        )
        return

    if item is None:
        await _answer_videoid(
            message,
            "No upload-item ID found for this video. "
            "It has not been registered yet.",
        )
        return

    await _answer_videoid(message, f"Video ID: {item.id}")
