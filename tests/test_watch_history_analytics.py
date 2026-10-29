from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from bot.db.models import Lecture, Progress, Subject, User
from bot.db.repo import recent_watch_history, subject_lecture_views, subject_view_overview
from bot.handlers.admin import analytics_cmd
from bot.handlers.progress import progress_entry
from bot.services.access import APPROVED


@pytest_asyncio.fixture
async def catalogue(session):
    anatomy = Subject(slug="anatomy", name="Anatomy", emoji="A", order=1)
    physiology = Subject(slug="physiology", name="Physiology", emoji="P", order=2)
    session.add_all([anatomy, physiology])
    await session.commit()
    await session.refresh(anatomy)
    await session.refresh(physiology)

    a1 = Lecture(subject_id=anatomy.id, title="Thorax", order=1)
    a2 = Lecture(subject_id=anatomy.id, title="Abdomen", order=2)
    p1 = Lecture(subject_id=physiology.id, title="Cells", order=1)
    session.add_all([a1, a2, p1])
    await session.commit()
    for lec in (a1, a2, p1):
        await session.refresh(lec)

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
    return {
        "anatomy": anatomy,
        "physiology": physiology,
        "a1": a1,
        "a2": a2,
        "p1": p1,
        "user": user,
    }


def _watch_row(user_id, lecture_id, when):
    return Progress(
        user_id=user_id, lecture_id=lecture_id, watched=True, watched_at=when
    )


def _message(text, user_id=9001):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, full_name="User"),
        answer=AsyncMock(),
    )


def _buttons(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


async def test_recent_watch_history_orders_by_recency(session, catalogue):
    user = catalogue["user"]
    now = datetime.utcnow()
    session.add_all(
        [
            _watch_row(user.id, catalogue["a1"].id, now - timedelta(days=2)),
            _watch_row(user.id, catalogue["a2"].id, now - timedelta(hours=1)),
        ]
    )
    await session.commit()

    history = await recent_watch_history(session, user.id, limit=5)
    assert [lecture.id for lecture, _, _ in history] == [
        catalogue["a2"].id,
        catalogue["a1"].id,
    ]
    assert [subject.slug for _, subject, _ in history] == ["anatomy", "anatomy"]

    limited = await recent_watch_history(session, user.id, limit=1)
    assert [lecture.id for lecture, _, _ in limited] == [catalogue["a2"].id]


async def test_progress_page_shows_recently_watched(session, catalogue):
    user = catalogue["user"]
    session.add(_watch_row(user.id, catalogue["a1"].id, datetime.utcnow()))
    await session.commit()

    message = _message("📈 Progress", user.id)
    await progress_entry(message, session, user)

    text = message.answer.await_args.args[0]
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert "Recently watched" in text
    assert "Thorax" in text
    assert "today" in text
    assert f"lec:{catalogue['a1'].id}" in _buttons(markup)


async def test_analytics_overview_reports_subject_totals(session, catalogue):
    session.add_all(
        [
            _watch_row(9101, catalogue["a1"].id, datetime.utcnow()),
            _watch_row(9102, catalogue["a1"].id, datetime.utcnow()),
            _watch_row(9103, catalogue["a2"].id, datetime.utcnow()),
            _watch_row(9104, catalogue["p1"].id, datetime.utcnow()),
        ]
    )
    await session.commit()

    message = _message("/analytics", user_id=123)
    await analytics_cmd(message, session)
    out = message.answer.await_args.args[0]
    assert "Lecture analytics" in out
    assert "Anatomy — 2 lectures · 3 views · avg 1.5" in out
    assert "Physiology — 1 lectures · 1 views" in out


async def test_analytics_subject_lists_lectures_by_views(session, catalogue):
    session.add(
        _watch_row(9101, catalogue["a1"].id, datetime.utcnow())
    )
    await session.commit()

    message = _message("/analytics anatomy", user_id=123)
    await analytics_cmd(message, session)
    out = message.answer.await_args.args[0]
    assert "Analytics — Anatomy" in out
    assert "Total views: 1" in out
    assert "1 lecture(s) with no views yet" in out
    lines = out.splitlines()
    thorax = next(i for i, line in enumerate(lines) if "Thorax" in line)
    abdomen = next(i for i, line in enumerate(lines) if "Abdomen" in line)
    assert thorax < abdomen
    assert "L1 — Thorax — 1 views" in out


async def test_analytics_unknown_subject(session, catalogue):
    message = _message("/analytics nope", user_id=123)
    await analytics_cmd(message, session)
    assert "not found" in message.answer.await_args.args[0]


async def test_analytics_ignores_non_admins(session, catalogue):
    message = _message("/analytics anatomy", user_id=9001)
    await analytics_cmd(message, session)
    message.answer.assert_not_awaited()


async def test_view_repo_helpers(session, catalogue):
    session.add_all(
        [
            _watch_row(9101, catalogue["a1"].id, datetime.utcnow()),
            _watch_row(9102, catalogue["a1"].id, datetime.utcnow()),
        ]
    )
    await session.commit()

    rows = await subject_lecture_views(session, catalogue["anatomy"].id)
    assert [(lecture.id, views) for lecture, views in rows] == [
        (catalogue["a1"].id, 2),
        (catalogue["a2"].id, 0),
    ]

    overview = await subject_view_overview(session)
    by_slug = {subject.slug: (lectures, views) for subject, lectures, views in overview}
    assert by_slug["anatomy"] == (2, 2)
    assert by_slug["physiology"] == (1, 0)
