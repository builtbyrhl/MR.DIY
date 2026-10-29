from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from bot.db.models import Lecture, Subject, UploadBatch, UploadItem, User
from bot.db.repo import (
    is_pinned,
    list_admin_actions,
    list_pinned_subject_ids,
    list_pinned_subjects,
    pin_subject,
    unpin_subject,
)
from bot.handlers.progress import progress_entry
from bot.handlers.review import batchops_cmd
from bot.handlers.subjects import cb_pin_subject, show_subjects
from bot.keyboards.inline import teachers_kb
from bot.services.access import APPROVED


@pytest_asyncio.fixture
async def catalogue(session):
    anatomy = Subject(slug="anatomy", name="Anatomy", emoji="A", order=1)
    surgery = Subject(slug="surgery", name="Surgery", emoji="S", order=2)
    session.add_all([anatomy, surgery])
    await session.commit()
    for subject in (anatomy, surgery):
        await session.refresh(subject)
    session.add_all(
        [
            Lecture(subject_id=anatomy.id, title="Thorax", order=1),
            Lecture(subject_id=surgery.id, title="Wound care", order=1),
        ]
    )
    user = User(
        id=9001,
        username="student",
        full_name="Student",
        is_premium=False,
        is_admin=False,
        access_status=APPROVED,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return {"anatomy": anatomy, "surgery": surgery, "user": user}


@pytest_asyncio.fixture
async def batch(session):
    row = UploadBatch(source_chat_id=-100, batch_date=date.today(), name="Week 1")
    session.add(row)
    await session.commit()
    await session.refresh(row)
    items = [
        UploadItem(
            batch_id=row.id,
            source_chat_id=-100,
            source_message_id=1,
            media_type="video",
            file_id="f1",
            status="ready",
        ),
        UploadItem(
            batch_id=row.id,
            source_chat_id=-100,
            source_message_id=2,
            media_type="video",
            file_id="f2",
            status="skipped",
        ),
        UploadItem(
            batch_id=row.id,
            source_chat_id=-100,
            source_message_id=3,
            media_type="video",
            file_id="f3",
            status="ready",
            publication_status="published",
            published_message_id=555,
        ),
        UploadItem(
            batch_id=row.id,
            source_chat_id=-100,
            source_message_id=4,
            media_type="video",
            file_id="f4",
            status="needs_review",
        ),
    ]
    session.add_all(items)
    await session.commit()
    for item in items:
        await session.refresh(item)
    return {"batch": row, "items": items}


def _message(text, user_id=123):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, full_name="Admin"),
        chat=SimpleNamespace(type="private"),
        answer=AsyncMock(),
    )


def _callback(data, user_id=9001):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, full_name="Student"),
        message=SimpleNamespace(edit_text=AsyncMock(), text="Anatomy"),
        answer=AsyncMock(),
    )


def _buttons(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def _labels(markup):
    return [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]


# ---------- Pinned subjects ----------
async def test_pin_subject_round_trip(session, catalogue):
    user = catalogue["user"]
    anatomy = catalogue["anatomy"]

    assert await pin_subject(session, user.id, anatomy.id) is True
    assert await is_pinned(session, user.id, anatomy.id) is True
    assert await list_pinned_subject_ids(session, user.id) == [anatomy.id]
    assert [s.slug for s in await list_pinned_subjects(session, user.id)] == [
        "anatomy"
    ]

    assert await pin_subject(session, user.id, anatomy.id) is True
    assert len(await list_pinned_subject_ids(session, user.id)) == 1

    assert await unpin_subject(session, user.id, anatomy.id) is True
    assert await is_pinned(session, user.id, anatomy.id) is False


async def test_pin_callback_toggles_pin(session, catalogue):
    user = catalogue["user"]
    anatomy = catalogue["anatomy"]

    cb = _callback(f"pin:{anatomy.id}")
    await cb_pin_subject(cb, session, user)
    assert await is_pinned(session, user.id, anatomy.id) is True
    assert "Pinned" in cb.answer.await_args.args[0]

    await cb_pin_subject(cb, session, user)
    assert await is_pinned(session, user.id, anatomy.id) is False


async def test_teacher_groups_keyboard_has_pin_button():
    kb = teachers_kb([], 5, pinned=False)
    assert "pin:5" in _buttons(kb)
    assert any("Pin subject" in label for label in _labels(kb))

    kb = teachers_kb([], 5, pinned=True)
    assert any("Unpin subject" in label for label in _labels(kb))


async def test_subject_list_shows_pinned_first(session, catalogue):
    user = catalogue["user"]
    await pin_subject(session, user.id, catalogue["surgery"].id)

    message = _message("📚 Subjects")
    await show_subjects(message, session, user_id=user.id)
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _buttons(markup)[0] == f"subj:{catalogue['surgery'].slug}"
    assert any("📌" in label for label in _labels(markup))


async def test_progress_page_has_pinned_section(session, catalogue):
    user = catalogue["user"]
    await pin_subject(session, user.id, catalogue["anatomy"].id)

    message = _message("📈 Progress", user.id)
    await progress_entry(message, session, user)
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert f"subj:{catalogue['anatomy'].slug}" in _buttons(markup)
    assert "Pinned" in message.answer.await_args.args[0]


# ---------- Bulk batch actions ----------
async def test_batchops_report_counts(session, batch):
    msg = _message(f"/batchops {batch['batch'].id}|report")
    await batchops_cmd(msg, session, AsyncMock())
    out = msg.answer.await_args.args[0]
    assert f"Batch #{batch['batch'].id}" in out
    assert "Ready: 1" in out
    assert "Published: 1" in out
    assert "Skipped: 1" in out
    assert "Unresolved: 1" in out
    assert f"#{batch['items'][3].id}" in out


async def test_batchops_approve_refuses_unresolved(session, batch):
    msg = _message(f"/batchops {batch['batch'].id}|approve")
    await batchops_cmd(msg, session, AsyncMock())
    out = msg.answer.await_args.args[0]
    assert "Cannot approve" in out
    await session.refresh(batch["batch"])
    assert batch["batch"].status != "approved"


async def test_batchops_skip_clears_unresolved_and_approves(session, batch):
    batch_id = batch["batch"].id
    msg = _message(f"/batchops {batch_id}|skip")
    await batchops_cmd(msg, session, AsyncMock())
    out = msg.answer.await_args.args[0]
    assert "Skipped 1 item(s)." in out
    await session.refresh(batch["items"][3])
    assert batch["items"][3].status == "skipped"

    msg = _message(f"/batchops {batch_id}|approve")
    await batchops_cmd(msg, session, AsyncMock())
    out = msg.answer.await_args.args[0]
    assert "Batch approved." in out
    await session.refresh(batch["batch"])
    assert batch["batch"].status == "approved"

    actions = await list_admin_actions(session, 10)
    assert [a.action for a in actions] == ["approve_batch", "skip_batch"]


async def test_batchops_refuses_skip_when_approved(session, batch):
    batch["batch"].status = "approved"
    await session.commit()
    msg = _message(f"/batchops {batch['batch'].id}|skip")
    await batchops_cmd(msg, session, AsyncMock())
    out = msg.answer.await_args.args[0]
    assert "refused" in out
    await session.refresh(batch["items"][3])
    assert batch["items"][3].status == "needs_review"


async def test_batchops_usage_mentions_actions(session):
    msg = _message("/batchops nope")
    await batchops_cmd(msg, session, AsyncMock())
    out = msg.answer.await_args.args[0]
    assert "Usage: /batchops" in out
    assert "ship" in out
