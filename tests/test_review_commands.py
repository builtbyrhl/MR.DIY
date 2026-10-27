from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest

from bot.db.ingestion_repo import approve_batch, get_batch, skip_item
from bot.db.models import Lecture
from bot.db.repo import add_teacher, add_topic
from bot.handlers import review
from bot.services.ingestion import ingest_media, set_item_caption

PREFERRED_CAPTION = "Anatomy A | Dr A | Skull | Introduction"
PREFERRED_CAPTION_WITH_NUMBER = (
    "Anatomy A | Dr A | Skull | Introduction | L2"
)
STALE_CAPTION = "Anatomy A | Skull | Lecture 2 | Dr A"


async def _prepared_item(
    session,
    seeded_course,
    *,
    message_id=60,
    caption=STALE_CAPTION,
    unique_id=None,
):
    subject, _ = seeded_course
    teacher, error = await add_teacher(session, subject.id, "Dr A")
    assert error is None
    topic, error = await add_topic(session, subject.id, "Skull")
    assert error is None
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type="video",
        caption=caption,
        file_name=None,
        file_unique_id=unique_id or f"refresh-{message_id}",
    )
    return item, teacher, topic


def _admin_message(batch_id, *, apply=False):
    suffix = " apply" if apply else ""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/refreshcaptions {batch_id}{suffix}",
        answer=AsyncMock(),
    )


def _answer_text(message):
    return "\n".join(call.args[0] for call in message.answer.await_args_list)


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


async def test_setcaption_rejects_skipped_item_without_reviving_it(
    session, seeded_course
):
    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=51,
        media_type="video",
        caption="Anatomy A Lecture 51",
        file_name=None,
        file_unique_id="skipped-setcaption",
    )
    assert await skip_item(session, item.id) is True
    original_caption = item.raw_caption

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setcaption {item.id}|Anatomy A Lecture 99",
        answer=AsyncMock(),
    )
    await review.setcaption_cmd(message, session)

    message.answer.assert_awaited_once()
    assert "skipped" in message.answer.await_args.args[0].lower()
    await session.refresh(item)
    assert item.status == "skipped"
    assert item.raw_caption == original_caption


async def test_setcaption_usage_is_sent_as_plain_text():
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text="/setcaption",
        answer=AsyncMock(),
    )

    await review.setcaption_cmd(message, None)

    message.answer.assert_awaited_once()
    assert "<item_id>" in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs == {"parse_mode": None}


async def test_setcaption_success_reply_uses_plain_text_for_special_titles():
    item = SimpleNamespace(
        id=77,
        status="needs_review",
        lecture_number=8,
        title="_Cardio_ *Exam* [Part 1] `code`",
        duplicate_flag=False,
        review_reason=None,
        teacher_review_reason=None,
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setcaption {item.id}|Anatomy A Lecture 8",
        answer=AsyncMock(),
    )
    set_caption = AsyncMock(return_value=(item, None))

    with patch.object(review, "set_item_caption", set_caption):
        await review.setcaption_cmd(message, object())

    set_caption.assert_awaited_once()
    message.answer.assert_awaited_once()
    assert item.title in message.answer.await_args.args[0]
    assert message.answer.await_args.kwargs == {"parse_mode": None}


async def test_setlecturetitle_updates_without_reupload(
    session, seeded_course
):
    subject, _ = seeded_course
    lecture = Lecture(subject_id=subject.id, title="Anatomy", order=1)
    session.add(lecture)
    await session.commit()
    await session.refresh(lecture)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setlecturetitle {lecture.id}|Brainstem",
        answer=AsyncMock(),
    )
    await review.setlecturetitle_cmd(message, session)

    await session.refresh(lecture)
    assert lecture.title == "Brainstem"
    assert "Brainstem" in message.answer.await_args.args[0]


async def test_keep_restores_skipped_item_before_caption_edit(
    session, seeded_course
):
    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=52,
        media_type="video",
        caption="Anatomy A Lecture 52",
        file_name=None,
        file_unique_id="keep-setcaption",
    )
    assert await skip_item(session, item.id) is True

    keep_message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/keep {item.id}",
        answer=AsyncMock(),
    )
    await review.keep_cmd(keep_message, session)
    await session.refresh(item)
    assert item.status != "skipped"

    caption_message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/setcaption {item.id}|Anatomy A Lecture 53",
        answer=AsyncMock(),
    )
    await review.setcaption_cmd(caption_message, session)

    await session.refresh(item)
    assert item.raw_caption == "Anatomy A Lecture 53"
    assert item.lecture_number == 53
    assert "updated" in caption_message.answer.await_args.args[0].lower()


async def test_refreshcaptions_preview_reports_source_without_changes(
    session, seeded_course
):
    item, _, _ = await _prepared_item(session, seeded_course)
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=900, caption=PREFERRED_CAPTION
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id)

    await review.refreshcaptions_cmd(message, session, bot)

    text = _answer_text(message)
    assert "Caption refresh preview" in text
    assert "No database changes made" in text
    assert STALE_CAPTION in text
    assert PREFERRED_CAPTION in text
    bot.forward_message.assert_awaited_once_with(
        chat_id=123,
        from_chat_id=-1001,
        message_id=item.source_message_id,
        disable_notification=True,
    )
    bot.delete_message.assert_awaited_once_with(
        chat_id=123, message_id=900
    )
    assert all(
        call.kwargs == {"parse_mode": None}
        for call in message.answer.await_args_list
    )

    await session.refresh(item)
    assert item.raw_caption == STALE_CAPTION
    assert item.original_caption == STALE_CAPTION
    assert item.status == "classified"


async def test_refreshcaptions_apply_updates_non_preferred_caption(
    session, seeded_course
):
    item, teacher, topic = await _prepared_item(session, seeded_course)
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=901, caption=PREFERRED_CAPTION
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == PREFERRED_CAPTION_WITH_NUMBER
    assert item.original_caption == STALE_CAPTION
    assert item.title == "Introduction"
    assert item.lecture_number == 2
    assert item.teacher_id == teacher.id
    assert item.status == "classified"
    assert "1 updated" in _answer_text(message)


async def test_refreshcaptions_apply_preserves_valid_manual_caption_and_reclassifies(
    session, seeded_course
):
    item, _, _ = await _prepared_item(session, seeded_course)
    item, error = await set_item_caption(session, item.id, PREFERRED_CAPTION)
    assert error is None
    item.status = "needs_review"
    item.review_reason = "topic not found: Skull"
    item.topic_review_reason = "topic not found: Skull"
    await session.commit()

    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=902,
        caption="Anatomy A | Dr A | Skull | Other Title",
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == PREFERRED_CAPTION
    assert item.original_caption == STALE_CAPTION
    assert item.title == "Introduction"
    assert item.status == "classified"
    text = _answer_text(message)
    assert "1 reclassified" in text
    assert "current caption reclassified" in text


async def test_refreshcaptions_apply_keeps_already_correct_caption(
    session, seeded_course
):
    item, _, _ = await _prepared_item(
        session, seeded_course, caption=PREFERRED_CAPTION
    )
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=903, caption=STALE_CAPTION
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == PREFERRED_CAPTION
    assert item.status == "classified"
    assert "1 preserved" in _answer_text(message)


async def test_refreshcaptions_apply_handles_partial_forward_failures(
    session, seeded_course
):
    first, _, _ = await _prepared_item(
        session, seeded_course, message_id=70
    )
    second, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=71,
        media_type="video",
        caption=STALE_CAPTION,
        file_name=None,
        file_unique_id="refresh-71",
    )

    async def forward(**kwargs):
        if kwargs["message_id"] == 71:
            raise TelegramBadRequest(None, "message to forward not found")
        return SimpleNamespace(message_id=904, caption=PREFERRED_CAPTION)

    bot = AsyncMock()
    bot.forward_message.side_effect = forward
    bot.delete_message.return_value = True
    message = _admin_message(first.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(first)
    await session.refresh(second)
    assert first.raw_caption == PREFERRED_CAPTION_WITH_NUMBER
    assert first.lecture_number == 2
    assert second.raw_caption == STALE_CAPTION
    text = _answer_text(message)
    assert "1 updated" in text
    assert "1 skipped" in text
    assert "unavailable" in text
    bot.delete_message.assert_awaited_once_with(
        chat_id=123, message_id=904
    )


async def test_refreshcaptions_apply_refuses_approved_batch(
    session, seeded_course
):
    item, _, _ = await _prepared_item(session, seeded_course)
    ok, reason = await approve_batch(session, item.batch_id, 123)
    assert ok is True, reason

    bot = AsyncMock()
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    text = _answer_text(message)
    assert "approved" in text
    assert "no changes" in text
    bot.forward_message.assert_not_awaited()
    await session.refresh(item)
    assert item.raw_caption == STALE_CAPTION


async def test_refreshcaptions_apply_skips_unusable_source_caption(
    session, seeded_course
):
    item, _, _ = await _prepared_item(session, seeded_course)
    bot = AsyncMock()
    bot.forward_message.return_value = SimpleNamespace(
        message_id=905, caption="not a structured caption"
    )
    bot.delete_message.return_value = True
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == STALE_CAPTION
    text = _answer_text(message)
    assert "1 skipped" in text
    assert "preferred layout" in text


async def test_refreshcaptions_apply_reclassifies_valid_caption_without_source(
    session, seeded_course
):
    item, _, _ = await _prepared_item(
        session, seeded_course, caption=PREFERRED_CAPTION
    )
    item.status = "needs_review"
    item.review_reason = "teacher not found: Introduction"
    item.teacher_review_reason = "teacher not found: Introduction"
    await session.commit()

    bot = AsyncMock()
    bot.forward_message.side_effect = TelegramBadRequest(
        None, "message to forward not found"
    )
    message = _admin_message(item.batch_id, apply=True)

    await review.refreshcaptions_cmd(message, session, bot)

    await session.refresh(item)
    assert item.raw_caption == PREFERRED_CAPTION
    assert item.status == "classified"
    text = _answer_text(message)
    assert "1 reclassified" in text
    assert "source unavailable" in text


async def test_manual_batch_collection_flow(monkeypatch, session, seeded_course):
    seeded_course
    monkeypatch.setenv("UPLOAD_INBOX_CHAT_ID", "-1001")

    create_msg = SimpleNamespace(from_user=SimpleNamespace(id=123), text="/createbatch Embryology", answer=AsyncMock())
    await review.createbatch_cmd(create_msg, session)
    create_text = create_msg.answer.await_args.args[0]
    assert "Created batch" in create_text
    batch_id = int(create_text.split()[2].replace("#", ""))

    start_msg = SimpleNamespace(from_user=SimpleNamespace(id=123), text=f"/startbatch {batch_id}", answer=AsyncMock())
    await review.startbatch_cmd(start_msg, session)
    assert "Started collecting" in start_msg.answer.await_args.args[0]

    active_msg = SimpleNamespace(from_user=SimpleNamespace(id=123), text="/activebatch", answer=AsyncMock())
    await review.activebatch_cmd(active_msg, session)
    assert "Active collecting" in active_msg.answer.await_args.args[0]

    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=500,
        media_type="video",
        caption="Anatomy A Lecture 1",
        file_name=None,
        file_unique_id="manual-batch-1",
    )
    assert item.batch_id == batch_id

    finish_msg = SimpleNamespace(from_user=SimpleNamespace(id=123), text="/finishbatch", answer=AsyncMock())
    await review.finishbatch_cmd(finish_msg, session)
    finish_text = finish_msg.answer.await_args.args[0]
    assert "Finished collection" in finish_text
    assert "approved or published" in finish_text.lower()

    batch = await get_batch(session, batch_id)
    assert batch.status == "collection_finished"
    await session.refresh(item)
    assert item.status in {"classified", "needs_review"}
    assert item.publication_status is None


async def test_startbatch_rejects_conflicting_active_batch(monkeypatch, session, seeded_course):
    seeded_course
    monkeypatch.setenv("UPLOAD_INBOX_CHAT_ID", "-1001")

    create1 = SimpleNamespace(from_user=SimpleNamespace(id=123), text="/createbatch Batch1", answer=AsyncMock())
    await review.createbatch_cmd(create1, session)
    batch1 = int(create1.answer.await_args.args[0].split()[2].replace("#", ""))
    await review.startbatch_cmd(SimpleNamespace(from_user=SimpleNamespace(id=123), text=f"/startbatch {batch1}", answer=AsyncMock()), session)

    create2 = SimpleNamespace(from_user=SimpleNamespace(id=123), text="/createbatch Batch2", answer=AsyncMock())
    await review.createbatch_cmd(create2, session)
    batch2 = int(create2.answer.await_args.args[0].split()[2].replace("#", ""))

    start2 = SimpleNamespace(from_user=SimpleNamespace(id=123), text=f"/startbatch {batch2}", answer=AsyncMock())
    await review.startbatch_cmd(start2, session)
    assert "already collecting" in start2.answer.await_args.args[0].lower()


async def test_ingestion_falls_back_to_daily_batch_when_no_active_collection(monkeypatch, session, seeded_course):
    seeded_course
    monkeypatch.setenv("UPLOAD_INBOX_CHAT_ID", "-1001")

    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=501,
        media_type="video",
        caption="Anatomy A Lecture 2",
        file_name=None,
        file_unique_id="daily-fallback-1",
    )
    batch = await get_batch(session, item.batch_id)
    assert batch.status == "open"
    assert batch.name is None


async def test_manual_batch_collection_crosses_utc_date(monkeypatch, session, seeded_course):
    seeded_course
    monkeypatch.setenv("UPLOAD_INBOX_CHAT_ID", "-1001")

    create_msg = SimpleNamespace(from_user=SimpleNamespace(id=123), text="/createbatch CrossDate", answer=AsyncMock())
    await review.createbatch_cmd(create_msg, session)
    batch_id = int(create_msg.answer.await_args.args[0].split()[2].replace("#", ""))
    await review.startbatch_cmd(SimpleNamespace(from_user=SimpleNamespace(id=123), text=f"/startbatch {batch_id}", answer=AsyncMock()), session)

    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=502,
        media_type="video",
        caption="Anatomy A Lecture 3",
        file_name=None,
        file_unique_id="cross-date-1",
    )
    assert item.batch_id == batch_id


async def test_non_admin_cannot_use_manual_batch_commands(session, seeded_course):
    seeded_course

    for text, cmd in (
        ("/createbatch X", review.createbatch_cmd),
        ("/startbatch 1", review.startbatch_cmd),
        ("/finishbatch", review.finishbatch_cmd),
        ("/activebatch", review.activebatch_cmd),
    ):
        msg = SimpleNamespace(from_user=SimpleNamespace(id=999), text=text, answer=AsyncMock())
        await cmd(msg, session)
        msg.answer.assert_not_awaited()


async def test_batch_command_falls_back_to_plain_text_on_bad_request(session, seeded_course):
    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=700,
        media_type="video",
        caption="Anatomy A Lecture 1",
        file_name=None,
        file_unique_id="batch-badreq-1",
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/batch {item.batch_id}",
        answer=AsyncMock(side_effect=[TelegramBadRequest(None, "can't parse entities"), None]),
    )
    await review.batch_cmd(message, session)
    assert message.answer.await_count == 2
    assert message.answer.await_args_list[1].kwargs.get("parse_mode") is None


async def test_preferred_video_title_allows_descriptive_part_titles():
    from bot.services import ingestion

    assert ingestion._preferred_video_title_error(
        "Embryological Development of the Gut - Part 1", "Embryology", "Gut"
    ) is None
    assert ingestion._preferred_video_title_error(
        "Embryological Development of the Midgut - Part 2", "Embryology", "Midgut"
    ) is None
    assert ingestion._preferred_video_title_error(
        "Embryological Development of the Hindgut and Urogenital System - Part 3",
        "Embryology",
        "Hindgut",
    ) is None


async def test_preferred_video_title_still_rejects_generic():
    from bot.services import ingestion

    assert ingestion._preferred_video_title_error("Lecture 5", "Embryology", "Gut") == "generic video title: Lecture 5"
    assert ingestion._preferred_video_title_error("Part 1", "Embryology", "Gut") == "generic video title: Part 1"
    assert ingestion._preferred_video_title_error("L1", "Embryology", "Gut") == "generic video title: L1"
    assert ingestion._preferred_video_title_error("Embryology", "Embryology", "Gut") == "generic video title: Embryology"
    assert ingestion._preferred_video_title_error("Gut", "Embryology", "Gut") == "generic video title: Gut"


async def test_descriptive_part_titles_classify_without_generic_review_reason_pipe_numbered(
    session, seeded_course
):
    from bot.services.ingestion import ingest_media
    from bot.db.ingestion_repo import get_batch

    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=600,
        media_type="video",
        caption="Embryology | Dr A | Gut | Embryological Development of the Gut - Part 1 | L5",
        file_name=None,
        file_unique_id="descriptive-part-pipe-1",
    )
    await session.refresh(item)
    batch = await get_batch(session, item.batch_id)
    assert batch.status == "open"
    assert "generic video title" not in (item.review_reason or "")
    assert item.status in {"classified", "needs_review"}


async def test_descriptive_part_titles_classify_without_generic_review_reason_slash_numbered(
    session, seeded_course
):
    from bot.services.ingestion import ingest_media
    from bot.db.ingestion_repo import get_batch

    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=601,
        media_type="video",
        caption="Embryology / Dr A / Midgut / Embryological Development of the Midgut - Part 2 / L6",
        file_name=None,
        file_unique_id="descriptive-part-slash-1",
    )
    await session.refresh(item)
    batch = await get_batch(session, item.batch_id)
    assert batch.status == "open"
    assert "generic video title" not in (item.review_reason or "")
    assert item.status in {"classified", "needs_review"}


async def test_descriptive_part_titles_classify_without_generic_review_reason_pipe_preferred(
    session, seeded_course
):
    from bot.services.ingestion import ingest_media

    seeded_course
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=602,
        media_type="video",
        caption="Embryology | Dr A | Hindgut and Urogenital System | Embryological Development of the Hindgut and Urogenital System - Part 3",
        file_name=None,
        file_unique_id="descriptive-part-pipe-pref-1",
    )
    await session.refresh(item)
    assert "generic video title" not in (item.review_reason or "")
