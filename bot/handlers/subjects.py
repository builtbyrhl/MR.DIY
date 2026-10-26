from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.utils.markdown import markdown_decoration

from bot.db.models import Subject, Teacher
from bot.db.repo import (
    TEACHER_UNSPECIFIED,
    get_subject,
    get_teacher,
    get_topic,
    list_lectures,
    list_lectures_by_teacher,
    list_lectures_by_teacher_topic,
    list_lectures_by_topic,
    list_subject_lecture_counts,
    list_subject_teacher_groups,
    list_subject_topic_groups,
    list_subjects,
    list_teacher_topic_groups,
    list_watched_lecture_ids,
    subject_progress,
)
from bot.keyboards.inline import (
    lectures_kb,
    subjects_kb,
    teacher_lectures_kb,
    teacher_topic_lectures_kb,
    teacher_topics_kb,
    teachers_kb,
    topic_lectures_kb,
    topics_kb,
)
from bot.services.catalog import ensure_subjects
from bot.student_ui import page_suffix, progress_line

router = Router()
logger = logging.getLogger(__name__)

TEACHERS_PER_PAGE = 8
TOPICS_PER_PAGE = 8
LECTURES_PER_PAGE = 8


def _subject_text(subject, done: int, total: int, lecture_count: int) -> str:
    text = f"{subject.emoji} *{subject.name}*\n{progress_line(done, total)}"
    if lecture_count == 0:
        text += "\n\nNo lectures uploaded yet for this subject."
    return text


def _teacher_groups_text(
    subject, done: int, total: int, groups, has_topics: bool = False
) -> str:
    text = _subject_text(subject, done, total, total)
    if groups:
        text += "\n\nChoose a teacher to open their lectures."
        if has_topics:
            text += "\nOr browse the same lectures by topic below."
    else:
        text += "\n\nTry another subject, or check back soon."
    return text


def _teacher_lectures_text(
    subject, teacher_name: str, done: int, total: int, lecture_count: int
) -> str:
    text = (
        f"{subject.emoji} *{subject.name}*\n"
        f"👩‍🏫 {markdown_decoration.quote(teacher_name)}\n"
        f"{progress_line(done, total)}"
    )
    if lecture_count:
        text += "\n\nChoose a lecture to open it."
    else:
        text += "\n\nNo lectures for this teacher."
    return text


def _teacher_topics_text(
    subject, teacher_name: str, done: int, total: int, groups
) -> str:
    text = (
        f"{subject.emoji} *{subject.name}*\n"
        f"👩‍🏫 {markdown_decoration.quote(teacher_name)}\n"
        f"{progress_line(done, total)}"
    )
    if groups:
        text += "\n\nChoose a system/topic to open its lectures."
    else:
        text += "\n\nNo topic-wise lectures for this teacher."
    return text


def _teacher_topic_lectures_text(
    subject,
    teacher_name: str,
    topic_name: str,
    done: int,
    total: int,
    lecture_count: int,
) -> str:
    text = (
        f"{subject.emoji} *{subject.name}*\n"
        f"👩‍🏫 {markdown_decoration.quote(teacher_name)}\n"
        f"🗂 {markdown_decoration.quote(topic_name)}\n"
        f"{progress_line(done, total)}"
    )
    if lecture_count:
        text += "\n\nChoose a lecture to open it."
    else:
        text += "\n\nNo lectures for this system/topic."
    return text


def _topic_groups_text(subject, done: int, total: int, groups) -> str:
    text = _subject_text(subject, done, total, total)
    if groups:
        text += "\n\nChoose a topic to open its lectures."
    else:
        text += "\n\nNo topic-wise lectures are available yet."
    return text


def _topic_lectures_text(
    subject, topic_name: str, done: int, total: int, lecture_count: int
) -> str:
    text = (
        f"{subject.emoji} *{subject.name}*\n"
        f"🗂 {markdown_decoration.quote(topic_name)}\n"
        f"{progress_line(done, total)}"
    )
    if lecture_count:
        text += "\n\nChoose a lecture to open it."
    else:
        text += "\n\nNo lectures for this topic."
    return text


def _clamp_page(page: int, total: int, per_page: int) -> int:
    if total <= 0:
        return 0
    return min(max(page, 0), (total - 1) // per_page)


async def _show_teacher_groups(cb, session, subject, page: int) -> None:
    groups = await list_subject_teacher_groups(session, subject.id)
    topic_groups = await list_subject_topic_groups(session, subject.id)
    page = _clamp_page(page, len(groups), TEACHERS_PER_PAGE)
    d, t = await subject_progress(session, cb.from_user.id, subject.id)
    has_topics = bool(topic_groups)
    text = _teacher_groups_text(subject, d, t, groups, has_topics)
    text += page_suffix(page, len(groups), TEACHERS_PER_PAGE)
    kb = teachers_kb(
        groups,
        subject.id,
        page=page,
        per_page=TEACHERS_PER_PAGE,
        has_topics=has_topics,
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)


async def _show_topics(cb, session, subject, page: int) -> None:
    groups = await list_subject_topic_groups(session, subject.id)
    page = _clamp_page(page, len(groups), TOPICS_PER_PAGE)
    d, t = await subject_progress(session, cb.from_user.id, subject.id)
    text = _topic_groups_text(subject, d, t, groups)
    text += page_suffix(page, len(groups), TOPICS_PER_PAGE)
    kb = topics_kb(groups, subject.id, page=page, per_page=TOPICS_PER_PAGE)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)


async def _show_topic_lectures(
    cb, session, subject, topic_id: int, page: int
) -> bool:
    topic = await get_topic(session, topic_id)
    if topic is None or topic.subject_id != subject.id:
        await cb.answer("Topic not found", show_alert=True)
        return False

    lectures = await list_lectures_by_topic(
        session, subject.id, topic.id, include_premium=True
    )
    page = _clamp_page(page, len(lectures), LECTURES_PER_PAGE)
    teacher_names: dict[int, str] = {}
    for teacher_id in {
        lecture.teacher_id
        for lecture in lectures
        if lecture.teacher_id is not None
    }:
        teacher = await session.get(Teacher, teacher_id)
        if teacher is not None:
            teacher_names[teacher_id] = teacher.name

    d, t = await subject_progress(session, cb.from_user.id, subject.id)
    watched_ids = await list_watched_lecture_ids(session, cb.from_user.id)
    text = _topic_lectures_text(
        subject, topic.name, d, t, len(lectures)
    )
    text += page_suffix(page, len(lectures), LECTURES_PER_PAGE)
    kb = topic_lectures_kb(
        lectures,
        subject.id,
        topic.id,
        page=page,
        per_page=LECTURES_PER_PAGE,
        teacher_names=teacher_names,
        watched_ids=watched_ids,
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    return True


async def _teacher_context(session, subject, teacher_id: int):
    if teacher_id == 0:
        return None, TEACHER_UNSPECIFIED
    teacher = await get_teacher(session, teacher_id)
    if teacher is None or teacher.subject_id != subject.id:
        return None, None
    return teacher, teacher.name


async def _show_teacher_lecture_list(
    cb,
    session,
    subject,
    teacher_id: int,
    page: int,
    nav_prefix: str = "tlec",
) -> bool:
    selected_teacher, teacher_name = await _teacher_context(
        session, subject, teacher_id
    )
    if teacher_name is None:
        await cb.answer("Teacher not found", show_alert=True)
        return False
    selected_id = selected_teacher.id if selected_teacher else None

    lectures = await list_lectures_by_teacher(
        session, subject.id, selected_id, include_premium=True
    )
    page = _clamp_page(page, len(lectures), LECTURES_PER_PAGE)
    d, t = await subject_progress(session, cb.from_user.id, subject.id)
    watched_ids = await list_watched_lecture_ids(session, cb.from_user.id)
    text = _teacher_lectures_text(
        subject, teacher_name, d, t, len(lectures)
    )
    text += page_suffix(page, len(lectures), LECTURES_PER_PAGE)
    kb = teacher_lectures_kb(
        lectures,
        subject.id,
        selected_id or 0,
        page=page,
        per_page=LECTURES_PER_PAGE,
        watched_ids=watched_ids,
        nav_prefix=nav_prefix,
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    return True


async def _show_teacher_topics(
    cb, session, subject, teacher_id: int, page: int
) -> bool:
    selected_teacher, teacher_name = await _teacher_context(
        session, subject, teacher_id
    )
    if teacher_name is None:
        await cb.answer("Teacher not found", show_alert=True)
        return False
    selected_id = selected_teacher.id if selected_teacher else None

    groups = await list_teacher_topic_groups(
        session, subject.id, selected_id
    )
    page = _clamp_page(page, len(groups), TOPICS_PER_PAGE)
    d, t = await subject_progress(session, cb.from_user.id, subject.id)
    text = _teacher_topics_text(subject, teacher_name, d, t, groups)
    text += page_suffix(page, len(groups), TOPICS_PER_PAGE)
    kb = teacher_topics_kb(
        groups,
        subject.id,
        teacher_id,
        page=page,
        per_page=TOPICS_PER_PAGE,
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    return True


async def _show_teacher_lectures(
    cb, session, subject, teacher_id: int, page: int
) -> bool:
    """Open a teacher dashboard: systems/topics first, else all lectures."""
    selected_teacher, teacher_name = await _teacher_context(
        session, subject, teacher_id
    )
    if teacher_name is None:
        await cb.answer("Teacher not found", show_alert=True)
        return False
    selected_id = selected_teacher.id if selected_teacher else None
    groups = await list_teacher_topic_groups(
        session, subject.id, selected_id
    )
    if groups:
        return await _show_teacher_topics(
            cb, session, subject, teacher_id, page
        )
    return await _show_teacher_lecture_list(
        cb, session, subject, teacher_id, page
    )


async def _show_teacher_topic_lectures(
    cb,
    session,
    subject,
    teacher_id: int,
    topic_id: int,
    page: int,
) -> bool:
    selected_teacher, teacher_name = await _teacher_context(
        session, subject, teacher_id
    )
    if teacher_name is None:
        await cb.answer("Teacher not found", show_alert=True)
        return False
    topic = await get_topic(session, topic_id)
    if topic is None or topic.subject_id != subject.id:
        await cb.answer("Topic not found", show_alert=True)
        return False

    selected_id = selected_teacher.id if selected_teacher else None
    lectures = await list_lectures_by_teacher_topic(
        session,
        subject.id,
        selected_id,
        topic.id,
        include_premium=True,
    )
    page = _clamp_page(page, len(lectures), LECTURES_PER_PAGE)
    d, t = await subject_progress(session, cb.from_user.id, subject.id)
    watched_ids = await list_watched_lecture_ids(session, cb.from_user.id)
    text = _teacher_topic_lectures_text(
        subject,
        teacher_name,
        topic.name,
        d,
        t,
        len(lectures),
    )
    text += page_suffix(page, len(lectures), LECTURES_PER_PAGE)
    kb = teacher_topic_lectures_kb(
        lectures,
        subject.id,
        teacher_id,
        topic.id,
        page=page,
        per_page=LECTURES_PER_PAGE,
        watched_ids=watched_ids,
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    return True


@router.message(F.text == "📚 My Courses")
@router.message(F.text == "📚 Subjects")
async def subjects_cmd(message: Message, session):
    await ensure_subjects(session)
    await show_subjects(message, session)


@router.message(Command("subjects"))
async def subjects_command(message: Message, session):
    await ensure_subjects(session)
    await show_subjects(message, session)


async def show_subjects(cb_or_msg, session, page: int = 0):
    subjects = await list_subjects(session)
    page = _clamp_page(page, len(subjects), 8)
    counts = await list_subject_lecture_counts(session)
    kb = subjects_kb(subjects, page=page, counts=counts)
    if subjects:
        text = "📚 *Subjects*\nChoose a subject to start studying."
        text += page_suffix(page, len(subjects), 8)
    else:
        text = "📚 *Subjects*\nNo subjects are available yet."
    if hasattr(cb_or_msg, "message"):
        await cb_or_msg.message.edit_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await cb_or_msg.answer(text, parse_mode="Markdown", reply_markup=kb)


async def _answer_subject_error(cb: CallbackQuery, message: str) -> None:
    logger.exception(message)
    await cb.answer("Could not load lectures. Please try again.", show_alert=True)


@router.callback_query(F.data == "subjects")
async def cb_subjects(cb: CallbackQuery, session):
    try:
        await show_subjects(cb, session)
    except Exception:
        await _answer_subject_error(cb, "Failed to show subject list")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("subjpage:"))
async def cb_subjpage(cb: CallbackQuery, session):
    try:
        page = int(cb.data.split(":")[1])
        await show_subjects(cb, session, page=page)
    except Exception:
        await _answer_subject_error(cb, "Failed to change subject page")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("subj:"))
async def open_subject(cb: CallbackQuery, session, db_user):
    slug = cb.data.split(":", 1)[1]
    try:
        subject = await get_subject(session, slug)
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        await _show_teacher_groups(cb, session, subject, 0)
    except Exception:
        await _answer_subject_error(cb, f"Failed to open subject {slug}")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("teachers:"))
async def teacher_page(cb: CallbackQuery, session, db_user):
    try:
        _, subject_raw, page_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        await _show_teacher_groups(cb, session, subject, int(page_raw))
    except Exception:
        await _answer_subject_error(cb, f"Failed to list teachers for {cb.data}")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("tlec:"))
async def open_teacher_lectures(cb: CallbackQuery, session, db_user):
    try:
        _, subject_raw, teacher_raw, page_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        shown = await _show_teacher_lectures(
            cb, session, subject, int(teacher_raw), int(page_raw)
        )
        if not shown:
            return
    except Exception:
        await _answer_subject_error(cb, f"Failed to list teacher lectures {cb.data}")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("back_teacher:"))
async def back_teacher(cb: CallbackQuery, session, db_user):
    try:
        parts = cb.data.split(":")
        if len(parts) not in {3, 4}:
            await cb.answer("Not found", show_alert=True)
            return
        _, subject_raw, teacher_raw = parts[:3]
        page = int(parts[3]) if len(parts) == 4 else 0
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        shown = await _show_teacher_lectures(
            cb, session, subject, int(teacher_raw), page
        )
        if not shown:
            return
    except Exception:
        await _answer_subject_error(cb, f"Failed to go back to {cb.data}")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("tlecflat:"))
async def open_teacher_lecture_list(cb: CallbackQuery, session, db_user):
    try:
        _, subject_raw, teacher_raw, page_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        shown = await _show_teacher_lecture_list(
            cb,
            session,
            subject,
            int(teacher_raw),
            int(page_raw),
            nav_prefix="tlecflat",
        )
        if not shown:
            return
    except Exception:
        await _answer_subject_error(
            cb, f"Failed to list all teacher lectures {cb.data}"
        )
        return
    await cb.answer()


@router.callback_query(F.data.startswith("ttopic:"))
async def open_teacher_topic_lectures(cb: CallbackQuery, session, db_user):
    try:
        _, subject_raw, teacher_raw, topic_raw, page_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        shown = await _show_teacher_topic_lectures(
            cb,
            session,
            subject,
            int(teacher_raw),
            int(topic_raw),
            int(page_raw),
        )
        if not shown:
            return
    except Exception:
        await _answer_subject_error(
            cb, f"Failed to list teacher topic lectures {cb.data}"
        )
        return
    await cb.answer()


@router.callback_query(F.data.startswith("back_teacher_topic:"))
async def back_teacher_topic(cb: CallbackQuery, session, db_user):
    try:
        _, subject_raw, teacher_raw, topic_raw, page_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        shown = await _show_teacher_topic_lectures(
            cb,
            session,
            subject,
            int(teacher_raw),
            int(topic_raw),
            int(page_raw),
        )
        if not shown:
            return
    except Exception:
        await _answer_subject_error(cb, f"Failed to go back to {cb.data}")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("topics:"))
async def topics_page(cb: CallbackQuery, session, db_user):
    try:
        _, subject_raw, page_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        await _show_topics(cb, session, subject, int(page_raw))
    except Exception:
        await _answer_subject_error(cb, f"Failed to list topics for {cb.data}")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("topicview:"))
async def open_topic_lectures(cb: CallbackQuery, session, db_user):
    try:
        _, subject_raw, topic_raw, page_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        shown = await _show_topic_lectures(
            cb, session, subject, int(topic_raw), int(page_raw)
        )
        if not shown:
            return
    except Exception:
        await _answer_subject_error(cb, f"Failed to list topic lectures {cb.data}")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("back_topic:"))
async def back_topic(cb: CallbackQuery, session, db_user):
    try:
        _, subject_raw, topic_raw, page_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        shown = await _show_topic_lectures(
            cb, session, subject, int(topic_raw), int(page_raw)
        )
        if not shown:
            return
    except Exception:
        await _answer_subject_error(cb, f"Failed to go back to {cb.data}")
        return
    await cb.answer()


@router.callback_query(F.data.startswith("lecpage:"))
async def lec_page(cb: CallbackQuery, session, db_user):
    try:
        _, slug, page = cb.data.split(":")
        subject = await get_subject(session, slug)
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        lectures = await list_lectures(session, subject.id, include_premium=True)
        page_number = _clamp_page(int(page), len(lectures), LECTURES_PER_PAGE)
        d, t = await subject_progress(session, db_user.id, subject.id)
        watched_ids = await list_watched_lecture_ids(session, db_user.id)
        text = _subject_text(subject, d, t, len(lectures))
        if lectures:
            text += "\n\nChoose a lecture to open it."
        text += page_suffix(page_number, len(lectures), LECTURES_PER_PAGE)
        kb = lectures_kb(
            lectures,
            subject.slug,
            page=page_number,
            watched_ids=watched_ids,
        )
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        await _answer_subject_error(cb, f"Failed to change lecture page for {cb.data}")
        return
    await cb.answer()
