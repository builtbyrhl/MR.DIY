import logging
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.db.models import UploadBatch, UploadItem
from bot.services.ingestion import ingest_media, parse_caption


def _ingest(
    session,
    *,
    message_id,
    caption=None,
    file_name=None,
    file_unique_id=None,
    media_type="video",
    source_chat_id=-1001,
):
    return ingest_media(
        session,
        source_chat_id=source_chat_id,
        source_message_id=message_id,
        media_type=media_type,
        caption=caption,
        file_name=file_name,
        file_unique_id=file_unique_id,
    )


async def test_valid_video_is_classified(session, seeded_course):
    subject, course = seeded_course

    item, created = await _ingest(
        session,
        message_id=1,
        caption="Anatomy A Lecture 01 - Upper Limb",
        file_unique_id="unique-video-1",
    )

    assert created is True
    assert item.media_type == "video"
    assert item.lecture_number == 1
    assert item.course_id == course.id
    assert item.status == "classified"
    assert item.review_reason is None


async def test_document_uses_filename(session, seeded_course):
    subject, course = seeded_course

    item, created = await _ingest(
        session,
        message_id=2,
        caption=None,
        file_name="Anatomy A Lecture 2.pdf",
        file_unique_id="unique-doc-1",
        media_type="document",
    )

    assert created is True
    assert item.media_type == "document"
    assert item.lecture_number == 2
    assert item.title == "Anatomy A Lecture 2"
    assert item.course_id == course.id
    assert item.status == "classified"


async def test_ambiguous_lecture_number_needs_review(session, seeded_course):
    seeded_course

    parsed = parse_caption("Anatomy A Lecture 1 and Lecture 2", None)
    assert parsed.numbers == [1, 2]
    assert parsed.lecture_number is None
    assert parsed.number_ambiguous is True

    item, _ = await _ingest(
        session,
        message_id=3,
        caption="Anatomy A Lecture 1 and Lecture 2",
        file_unique_id="ambiguous-1",
    )

    assert item.status == "needs_review"
    assert item.lecture_number is None
    assert "ambiguous lecture number" in item.review_reason


async def test_unknown_course_needs_review(session, seeded_course):
    seeded_course

    item, _ = await _ingest(
        session,
        message_id=4,
        caption="Lecture 5 - Something Else",
        file_unique_id="unknown-1",
    )

    assert item.status == "needs_review"
    assert item.course_id is None
    assert "course/year not confidently identified" in item.review_reason


async def test_duplicate_telegram_update_is_idempotent(session, seeded_course):
    seeded_course

    first, created_first = await _ingest(
        session, message_id=5, caption="Anatomy A Lecture 5", file_unique_id="dup-update"
    )
    second, created_second = await _ingest(
        session, message_id=5, caption="Anatomy A Lecture 5", file_unique_id="dup-update"
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    count = await session.scalar(select(func.count(UploadItem.id)))
    assert count == 1


async def test_duplicate_file_is_flagged(session, seeded_course):
    seeded_course

    first, _ = await _ingest(
        session, message_id=6, caption="Anatomy A Lecture 6", file_unique_id="same-file"
    )
    second, _ = await _ingest(
        session, message_id=7, caption="Anatomy A Lecture 7", file_unique_id="same-file"
    )

    assert second.duplicate_flag is True
    assert second.duplicate_of_item_id == first.id
    assert second.status == "needs_review"
    assert "suspected duplicate file" in second.review_reason


async def test_item_persists_in_new_session(db, session, seeded_course):
    seeded_course
    item, _ = await _ingest(
        session, message_id=8, caption="Anatomy A Lecture 8", file_unique_id="persist-1"
    )

    _, factory = db
    async with factory() as fresh_session:
        stored = await fresh_session.get(UploadItem, item.id)
        assert stored is not None
        assert stored.title == "Anatomy A Lecture 8"
        assert stored.status == "classified"


async def test_approval_blocks_unresolved_items(session, seeded_course):
    seeded_course
    item, _ = await _ingest(
        session, message_id=20, caption="Lecture 9 - Unknown Course", file_unique_id="block-1"
    )

    from bot.db.ingestion_repo import approve_batch

    ok, text = await approve_batch(session, item.batch_id, 123)
    assert ok is False
    assert "unresolved" in text


async def test_only_one_open_batch_per_source_and_date(session):
    batch_date = date(2026, 1, 15)
    session.add(
        UploadBatch(
            source_chat_id=-1001, batch_date=batch_date, status="open"
        )
    )
    await session.commit()
    session.add(
        UploadBatch(
            source_chat_id=-1001, batch_date=batch_date, status="open"
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    session.add_all(
        [
            UploadBatch(
                source_chat_id=-1001, batch_date=batch_date, status="approved"
            ),
            UploadBatch(
                source_chat_id=-1001, batch_date=batch_date, status="approved"
            ),
        ]
    )
    await session.commit()

    count = await session.scalar(
        select(func.count(UploadBatch.id)).where(
            UploadBatch.source_chat_id == -1001,
            UploadBatch.batch_date == batch_date,
        )
    )
    assert count == 3


async def test_upload_after_approval_creates_new_batch(session, seeded_course):
    seeded_course
    first, _ = await _ingest(
        session, message_id=21, caption="Anatomy A Lecture 21", file_unique_id="after-1"
    )

    from bot.db.ingestion_repo import approve_batch

    ok, _ = await approve_batch(session, first.batch_id, 123)
    assert ok is True

    second, _ = await _ingest(
        session, message_id=22, caption="Anatomy A Lecture 22", file_unique_id="after-2"
    )
    assert second.batch_id != first.batch_id


async def test_pause_and_resume_reclassifies_items(session, seeded_course):
    seeded_course
    from bot.services.ingestion import pause_ingestion, resume_ingestion

    await pause_ingestion(session)
    item, _ = await _ingest(
        session, message_id=23, caption="Anatomy A Lecture 23", file_unique_id="paused-1"
    )
    assert item.status == "paused"

    count = await resume_ingestion(session)
    assert count == 1
    await session.refresh(item)
    assert item.status == "classified"
    assert item.course_id is not None


async def test_admin_can_assign_course_and_number(session, seeded_course):
    subject, course = seeded_course
    item, _ = await _ingest(
        session, message_id=24, caption="Lecture 24 - Unknown", file_unique_id="manual-1"
    )
    assert item.status == "needs_review"

    from bot.db.ingestion_repo import set_item_course, set_item_number

    item, error = await set_item_course(session, item.id, course.id)
    assert error is None
    assert item.course_id == course.id

    item, error = await set_item_number(session, item.id, 24)
    assert error is None
    assert item.lecture_number == 24
    assert item.status == "classified"


def test_l2_anatomy_text_is_not_treated_as_lecture_number():
    parsed = parse_caption("L2 vertebra compression", None)
    assert parsed.numbers == []
    assert parsed.lecture_number is None
    assert parsed.number_ambiguous is False

    part = parse_caption("Part 2 - Upper Limb", None)
    assert part.lecture_number == 2


async def test_l2_anatomy_text_is_reviewed_not_numbered(session, seeded_course):
    seeded_course
    item, _ = await _ingest(
        session,
        message_id=25,
        caption="Anatomy A L2 vertebra compression",
        file_unique_id="l2-anatomy",
    )
    assert item.lecture_number is None
    assert item.status == "needs_review"
    assert "lecture number missing" in item.review_reason


async def test_duplicate_scope_is_source_chat_and_media_type(session, seeded_course):
    seeded_course
    first, _ = await _ingest(
        session, message_id=30, caption="Anatomy A Lecture 30", file_unique_id="shared-file"
    )
    other_chat, _ = await _ingest(
        session,
        message_id=31,
        caption="Anatomy A Lecture 31",
        file_unique_id="shared-file",
        source_chat_id=-2002,
    )
    other_type, _ = await _ingest(
        session,
        message_id=32,
        caption="Anatomy A Lecture 32",
        file_unique_id="shared-file",
        media_type="document",
    )

    assert first.duplicate_flag is False
    assert other_chat.duplicate_flag is False
    assert other_type.duplicate_flag is False

    same_scope, _ = await _ingest(
        session, message_id=33, caption="Anatomy A Lecture 33", file_unique_id="shared-file"
    )
    assert same_scope.duplicate_flag is True
    assert same_scope.duplicate_of_item_id == first.id


async def test_keep_only_clears_selected_item(session, seeded_course):
    seeded_course
    first, _ = await _ingest(
        session, message_id=34, caption="Anatomy A Lecture 34", file_unique_id="keep-scope"
    )
    second, _ = await _ingest(
        session, message_id=35, caption="Anatomy A Lecture 35", file_unique_id="keep-scope"
    )
    third, _ = await _ingest(
        session, message_id=36, caption="Anatomy A Lecture 36", file_unique_id="keep-scope"
    )
    assert second.duplicate_flag is True
    assert third.duplicate_flag is True

    from bot.services.ingestion import keep_item

    kept = await keep_item(session, second.id)
    await session.refresh(first)
    await session.refresh(third)

    assert kept.duplicate_flag is False
    assert kept.duplicate_of_item_id is None
    assert first.duplicate_flag is False
    assert third.duplicate_flag is True
    assert third.duplicate_of_item_id == first.id


async def test_approved_batch_items_are_immutable(session, seeded_course):
    subject, course = seeded_course
    item, _ = await _ingest(
        session, message_id=37, caption="Anatomy A Lecture 37", file_unique_id="immutable"
    )

    from bot.db.ingestion_repo import (
        approve_batch,
        set_item_course,
        set_item_number,
        skip_item,
    )
    from bot.errors import ApprovedBatchError
    from bot.services.ingestion import keep_item, retry_item

    ok, _ = await approve_batch(session, item.batch_id, 123)
    assert ok is True

    with pytest.raises(ApprovedBatchError):
        await skip_item(session, item.id)
    with pytest.raises(ApprovedBatchError):
        await set_item_course(session, item.id, course.id)
    with pytest.raises(ApprovedBatchError):
        await set_item_number(session, item.id, 99)
    with pytest.raises(ApprovedBatchError):
        await retry_item(session, item.id)
    with pytest.raises(ApprovedBatchError):
        await keep_item(session, item.id)


async def test_classification_failure_is_logged_and_preserved(
    session, seeded_course, monkeypatch, caplog
):
    seeded_course

    async def fail_classification(session, item):
        raise RuntimeError("simulated classification failure")

    monkeypatch.setattr("bot.services.ingestion.classify_item", fail_classification)

    with caplog.at_level(logging.ERROR, logger="bot.services.ingestion"):
        item, created = await _ingest(
            session,
            message_id=60,
            caption="Anatomy A Lecture 60",
            file_unique_id="classification-failure",
        )

    assert created is True
    assert item.status == "failed"
    assert "simulated classification failure" in item.error
    assert "Failed to classify upload item #" in caplog.text
