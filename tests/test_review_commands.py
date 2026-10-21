from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.db.ingestion_repo import approve_batch
from bot.handlers import review
from bot.services.ingestion import ingest_media


async def test_skip_command_explains_approved_batch(session, seeded_course):
    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=50,
        media_type="video",
        caption="Anatomy A Lecture 50",
        file_name=None,
        file_unique_id="approved-skip",
    )
    ok, _ = await approve_batch(session, item.batch_id, 123)
    assert ok is True

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/skip {item.id}",
        answer=AsyncMock(),
    )
    await review.skip_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "approved" in message.answer.await_args.args[0].lower()
