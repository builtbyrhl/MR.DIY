from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from bot.db.ingestion_repo import (
    approve_batch,
    set_item_course,
    set_item_teacher,
    set_item_topics,
    skip_item,
)
from bot.db.models import Course, Subject, UploadItem
from bot.db.repo import add_teacher, add_topic
from bot.handlers import review
from bot.services.ingestion import ingest_media, parse_filename


@pytest_asyncio.fixture
async def caption_catalogue(session):
    anatomy = Subject(slug="anatomy", name="Anatomy", emoji="🫀", order=1)
    session.add(anatomy)
    await session.commit()
    await session.refresh(anatomy)

    course = Course(year="", subject_id=anatomy.id, name="Anatomy", aliases="anatomy")
    session.add(course)
    await session.commit()
    await session.refresh(course)

    teacher, error = await add_teacher(session, anatomy.id, "Dr Mohammad Azam Sir")
    assert error is None
    abdomen, error = await add_topic(session, anatomy.id, "Abdomen")
    assert error is None
    skull, error = await add_topic(session, anatomy.id, "Skull")
    assert error is None
    return SimpleNamespace(
        anatomy=anatomy,
        course=course,
        teacher=teacher,
        abdomen=abdomen,
        skull=skull,
    )


async def _ingest(session, message_id, *, caption=None, file_name=None):
    item, _ = await ingest_media(
        session,
        source_chat_id=-1001,
        source_message_id=message_id,
        media_type="video",
        caption=caption,
        file_name=file_name,
        file_id=f"file-{message_id}",
        file_unique_id=f"unique-{message_id}",
    )
    return item


async def _assign_metadata(session, item, cat):
    item, error = await set_item_course(session, item.id, cat.course.id)
    assert error is None
    item, error = await set_item_teacher(session, item.id, cat.teacher.id)
    assert error is None
    item, error = await set_item_topics(session, item.id, [cat.abdomen.id])
    assert error is None
    return item


def _generate_message(batch_id, *, apply=False):
    suffix = " apply" if apply else ""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=f"/generatecaptions {batch_id}{suffix}",
        answer=AsyncMock(),
    )


def _answer_text(message):
    return "\n".join(call.args[0] for call in message.answer.await_args_list)


def test_parse_filename_extracts_leading_numbers_and_strips_extensions():
    for filename, number, title in (
        ("08 Posterior Triangle of Neck.mp4", 8, "Posterior Triangle of Neck"),
        ("8-Posterior.mp4", 8, "Posterior"),
        ("08_Posterior_Triangle.mkv", 8, "Posterior Triangle"),
        ("L08 Posterior.mp4", 8, "Posterior"),
        ("Lecture 08: Posterior.mp4", 8, "Posterior"),
        ("Video 8 - Posterior.mp4", 8, "Posterior"),
        ("Part 2: Posterior.mp4", 2, "Posterior"),
        ("8.Posterior.mp4", 8, "Posterior"),
        ("8#Posterior.mp4", 8, "Posterior"),
        ("8)Posterior.mp4", 8, "Posterior"),
        ("008 Posterior.mp4", 8, "Posterior"),
        (
            "C:\\videos\\08 Posterior Triangle of Neck.mp4",
            8,
            "Posterior Triangle of Neck",
        ),
    ):
        assert parse_filename(filename) == (number, title)


def test_parse_filename_cleans_trailing_atf_noise():
    for filename, number, title in (
        ("1. Lumbosacral Plexus atf.mp4", 1, "Lumbosacral Plexus"),
        (
            "1.  Lumbosacral   Plexus  atf.mp4",
            1,
            "Lumbosacral Plexus",
        ),
        ("2. Common Peroneal Nerve atf.mp4", 2, "Common Peroneal Nerve"),
        (
            "8. Gluteal Region Greater & Lesser Sciatic Foramen atf.mp4",
            8,
            "Gluteal Region Greater & Lesser Sciatic Foramen",
        ),
        (
            "10. Vessels of Lower Limb - Popliteal Fossa.mp4",
            10,
            "Vessels of Lower Limb - Popliteal Fossa",
        ),
    ):
        assert parse_filename(filename) == (number, title)


def test_parse_filename_preserves_meaningful_atf_like_tokens():
    for filename, number, title in (
        ("11. ATF Receptor Signaling.mp4", 11, "ATF Receptor Signaling"),
        (
            "12. Catfish and ATF3 Signaling.mp4",
            12,
            "Catfish and ATF3 Signaling",
        ),
        ("13. ATF.mp4", 13, "ATF"),
        ("14. Lipid Metabolism ATF.mp4", 14, "Lipid Metabolism ATF"),
        (
            "Posterior Triangle atf receptor.mp4",
            None,
            "Posterior Triangle atf receptor",
        ),
    ):
        assert parse_filename(filename) == (number, title)


def test_parse_filename_handles_missing_and_ambiguous_numbers_safely():
    assert parse_filename("Posterior Triangle.mp4") == (
        None,
        "Posterior Triangle",
    )
    assert parse_filename("08.mp4") == (8, None)
    assert parse_filename("1000 Posterior.mp4") == (None, None)
    assert parse_filename("1-2 Posterior.mp4") == (None, None)
    assert parse_filename("21st Anatomy.mp4") == (None, None)
    assert parse_filename(None) == (None, None)
    assert parse_filename(".mp4") == (None, None)


async def test_ingestion_uses_filename_number_and_title_when_caption_missing(
    session, caption_catalogue
):
    caption_catalogue
    item = await _ingest(
        session,
        1,
        caption=None,
        file_name="08 Posterior Triangle of Neck.mp4",
    )

    assert item.lecture_number == 8
    assert item.title == "Posterior Triangle of Neck"
    assert item.course_id is None
    assert item.status == "needs_review"
    assert "course/year not confidently identified" in item.review_reason


async def test_generate_captions_preserves_valid_existing_caption(
    session, caption_catalogue
):
    cat = caption_catalogue
    item = await _ingest(
        session,
        2,
        caption=(
            "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
        ),
        file_name="08 Liver Kidney.mp4",
    )
    assert item.status == "classified"

    preview = _generate_message(item.batch_id)
    await review.generatecaptions_cmd(preview, session)
    preview_text = _answer_text(preview)
    assert "action: preserve" in preview_text
    assert "already valid" in preview_text

    await session.refresh(item)
    assert item.raw_caption == (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
    )

    apply = _generate_message(item.batch_id, apply=True)
    await review.generatecaptions_cmd(apply, session)
    await session.refresh(item)

    assert item.raw_caption == (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
    )
    assert item.lecture_number == 8
    assert item.status == "classified"
    assert "1 preserved" in _answer_text(apply)


async def test_generate_captions_preview_and_apply_use_existing_metadata(
    session, caption_catalogue
):
    cat = caption_catalogue
    item = await _ingest(
        session,
        3,
        caption=None,
        file_name="08 Liver Kidney.mp4",
    )
    await _assign_metadata(session, item, cat)

    expected_caption = (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
    )
    preview = _generate_message(item.batch_id)
    await review.generatecaptions_cmd(preview, session)
    preview_text = _answer_text(preview)
    assert "action: update" in preview_text
    assert expected_caption in preview_text

    await session.refresh(item)
    assert item.raw_caption is None
    assert item.lecture_number == 8

    apply = _generate_message(item.batch_id, apply=True)
    await review.generatecaptions_cmd(apply, session)
    await session.refresh(item)

    assert item.raw_caption == expected_caption
    assert item.lecture_number == 8
    assert item.title == "Liver Kidney"
    assert item.status == "classified"
    assert item.publication_status is None
    apply_text = _answer_text(apply)
    assert "1 updated" in apply_text
    assert "caption generated" in apply_text


async def test_generate_captions_cleans_filenames_and_keeps_numbers_in_number_field(
    session, caption_catalogue
):
    cat = caption_catalogue
    cases = (
        ("1. Lumbosacral Plexus atf.mp4", 1, "Lumbosacral Plexus"),
        ("2. Common Peroneal Nerve atf.mp4", 2, "Common Peroneal Nerve"),
        (
            "8. Gluteal Region Greater & Lesser Sciatic Foramen atf.mp4",
            8,
            "Gluteal Region Greater & Lesser Sciatic Foramen",
        ),
        (
            "10. Vessels of Lower Limb - Popliteal Fossa.mp4",
            10,
            "Vessels of Lower Limb - Popliteal Fossa",
        ),
        ("11. ATF Receptor Signaling.mp4", 11, "ATF Receptor Signaling"),
        (
            "12. Catfish and ATF3 Signaling.mp4",
            12,
            "Catfish and ATF3 Signaling",
        ),
    )
    expected: dict[int, str] = {}
    for index, (file_name, number, title) in enumerate(cases, start=60):
        item = await _ingest(
            session, index, caption=None, file_name=file_name
        )
        await _assign_metadata(session, item, cat)
        expected[item.id] = (
            "Anatomy | Dr Mohammad Azam Sir | Abdomen | "
            f"{title} | L{number}"
        )

    preview = _generate_message(item.batch_id)
    await review.generatecaptions_cmd(preview, session)
    preview_text = _answer_text(preview)
    for caption in expected.values():
        assert caption in preview_text

    for item_id in expected:
        stored = await session.get(UploadItem, item_id)
        assert stored.raw_caption is None

    apply = _generate_message(item.batch_id, apply=True)
    await review.generatecaptions_cmd(apply, session)

    for item_id, caption in expected.items():
        stored = await session.get(UploadItem, item_id)
        assert stored.raw_caption == caption
        assert stored.lecture_number == int(caption.rsplit("L", 1)[1])
    assert "6 updated" in _answer_text(apply)


async def test_generate_captions_skips_missing_metadata(
    session, caption_catalogue
):
    cat = caption_catalogue

    no_course = await _ingest(
        session, 10, caption=None, file_name="01 First.mp4"
    )
    preview = _generate_message(no_course.batch_id)
    await review.generatecaptions_cmd(preview, session)
    assert "course/subject missing" in _answer_text(preview)

    no_teacher = await _ingest(
        session, 11, caption=None, file_name="02 Second.mp4"
    )
    no_teacher, error = await set_item_course(
        session, no_teacher.id, cat.course.id
    )
    assert error is None
    no_teacher, error = await set_item_topics(
        session, no_teacher.id, [cat.abdomen.id]
    )
    assert error is None
    preview = _generate_message(no_teacher.batch_id)
    await review.generatecaptions_cmd(preview, session)
    assert "teacher missing" in _answer_text(preview)

    no_topic = await _ingest(
        session, 12, caption=None, file_name="03 Third.mp4"
    )
    no_topic, error = await set_item_course(session, no_topic.id, cat.course.id)
    assert error is None
    no_topic, error = await set_item_teacher(session, no_topic.id, cat.teacher.id)
    assert error is None
    preview = _generate_message(no_topic.batch_id)
    await review.generatecaptions_cmd(preview, session)
    assert "topic missing" in _answer_text(preview)

    multiple_topics = await _ingest(
        session, 13, caption=None, file_name="04 Fourth.mp4"
    )
    multiple_topics = await _assign_metadata(session, multiple_topics, cat)
    multiple_topics, error = await set_item_topics(
        session, multiple_topics.id, [cat.abdomen.id, cat.skull.id]
    )
    assert error is None
    preview = _generate_message(multiple_topics.batch_id)
    await review.generatecaptions_cmd(preview, session)
    assert "multiple topics assigned" in _answer_text(preview)


async def test_generate_captions_skips_filenames_without_clear_number_or_title(
    session, caption_catalogue
):
    cat = caption_catalogue
    for message_id, file_name in (
        (20, "1000 Liver Kidney.mp4"),
        (21, "Liver Kidney.mp4"),
        (22, "08.mp4"),
    ):
        item = await _ingest(session, message_id, caption=None, file_name=file_name)
        await _assign_metadata(session, item, cat)

        preview = _generate_message(item.batch_id)
        await review.generatecaptions_cmd(preview, session)
        assert "filename has no clear" in _answer_text(preview)

        await session.refresh(item)
        assert item.raw_caption is None


async def test_generate_captions_flags_duplicate_lecture_numbers(
    session, caption_catalogue
):
    cat = caption_catalogue
    first = await _ingest(
        session, 30, caption=None, file_name="01 First.mp4"
    )
    second = await _ingest(
        session, 31, caption=None, file_name="01 Second.mp4"
    )
    await _assign_metadata(session, first, cat)
    await _assign_metadata(session, second, cat)

    apply = _generate_message(first.batch_id, apply=True)
    await review.generatecaptions_cmd(apply, session)

    await session.refresh(first)
    await session.refresh(second)
    assert first.lecture_number == 1
    assert second.lecture_number == 1
    assert "duplicate lecture number L1" in (first.review_reason or "")
    assert "duplicate lecture number L1" in (second.review_reason or "")
    assert "2 updated" in _answer_text(apply)


async def test_generate_captions_does_not_apply_to_approved_batch(
    session, caption_catalogue
):
    cat = caption_catalogue
    item = await _ingest(
        session,
        40,
        caption=(
            "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
        ),
        file_name="08 Liver Kidney.mp4",
    )
    ok, text = await approve_batch(session, item.batch_id, 123)
    assert ok is True, text

    message = _generate_message(item.batch_id, apply=True)
    await review.generatecaptions_cmd(message, session)

    await session.refresh(item)
    assert item.raw_caption == (
        "Anatomy | Dr Mohammad Azam Sir | Abdomen | Liver Kidney | L8"
    )
    assert item.lecture_number == 8
    assert "approved" in _answer_text(message)


async def test_generate_captions_does_not_apply_to_published_or_skipped_items(
    session, caption_catalogue
):
    cat = caption_catalogue
    published = await _ingest(
        session, 50, caption=None, file_name="05 Published.mp4"
    )
    published = await _assign_metadata(session, published, cat)
    published.status = "classified"
    published.publication_status = "published"
    await session.commit()

    preview = _generate_message(published.batch_id)
    await review.generatecaptions_cmd(preview, session)
    assert "publishing, published, or otherwise locked" in _answer_text(preview)

    skipped = await _ingest(
        session, 51, caption=None, file_name="06 Skipped.mp4"
    )
    assert await skip_item(session, skipped.id) is True

    preview = _generate_message(skipped.batch_id)
    await review.generatecaptions_cmd(preview, session)
    assert "item is skipped" in _answer_text(preview)

    apply = _generate_message(published.batch_id, apply=True)
    await review.generatecaptions_cmd(apply, session)
    await session.refresh(published)
    assert published.raw_caption is None
    assert "2 skipped" in _answer_text(apply)
