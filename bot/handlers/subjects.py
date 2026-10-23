from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.utils.markdown import markdown_decoration

from bot.db.models import Subject
from bot.db.repo import (
    TEACHER_UNSPECIFIED,
    get_subject,
    get_teacher,
    list_lectures,
    list_lectures_by_teacher,
    list_subject_teacher_groups,
    list_subjects,
    subject_progress,
)
from bot.keyboards.inline import (
    lectures_kb,
    subjects_kb,
    teacher_lectures_kb,
    teachers_kb,
)
from bot.services.catalog import ensure_subjects

router = Router()
logger = logging.getLogger(__name__)

TEACHERS_PER_PAGE = 8
LECTURES_PER_PAGE = 8


def _subject_text(subject, done: int, total: int, lecture_count: int) -> str:
    pct = int(done / total * 100) if total else 0
    text = f"{subject.emoji} *{subject.name}*\nProgress: {done}/{total} ({pct}%)"
    if lecture_count == 0:
        text += "\n\nNo lectures uploaded yet for this subject."
    return text


def _teacher_groups_text(subject, done: int, total: int, groups) -> str:
    text = _subject_text(subject, done, total, total)
    if groups:
        text += "\n\nChoose a teacher:"
    return text


def _teacher_lectures_text(
    subject, teacher_name: str, done: int, total: int, lecture_count: int
) -> str:
    pct = int(done / total * 100) if total else 0
    text = (
        f"{subject.emoji} *{subject.name}*\n"
        f"👩‍🏫 {markdown_decoration.quote(teacher_name)}\n"
        f"Progress: {done}/{total} ({pct}%)"
    )
    if lecture_count == 0:
        text += "\n\nNo lectures for this teacher."
    return text


def _clamp_page(page: int, total: int, per_page: int) -> int:
    if total <= 0:
        return 0
    return min(max(page, 0), (total - 1) // per_page)


async def _show_teacher_groups(cb, session, subject, page: int) -> None:
    groups = await list_subject_teacher_groups(session, subject.id)
    page = _clamp_page(page, len(groups), TEACHERS_PER_PAGE)
    d, t = await subject_progress(session, cb.from_user.id, subject.id)
    text = _teacher_groups_text(subject, d, t, groups)
    kb = teachers_kb(groups, subject.id, page=page, per_page=TEACHERS_PER_PAGE)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)


async def _show_teacher_lectures(
    cb, session, subject, teacher_id: int, page: int
) -> bool:
    if teacher_id == 0:
        selected_id = None
        teacher_name = TEACHER_UNSPECIFIED
    else:
        teacher = await get_teacher(session, teacher_id)
        if teacher is None or teacher.subject_id != subject.id:
            await cb.answer("Teacher not found", show_alert=True)
            return False
        selected_id = teacher.id
        teacher_name = teacher.name

    lectures = await list_lectures_by_teacher(
        session, subject.id, selected_id, include_premium=True
    )
    page = _clamp_page(page, len(lectures), LECTURES_PER_PAGE)
    d, t = await subject_progress(session, cb.from_user.id, subject.id)
    text = _teacher_lectures_text(
        subject, teacher_name, d, t, len(lectures)
    )
    kb = teacher_lectures_kb(
        lectures,
        subject.id,
        selected_id or 0,
        page=page,
        per_page=LECTURES_PER_PAGE,
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    return True


@router.message(F.text == "📚 Subjects")
async def subjects_cmd(message: Message, session):
    await ensure_subjects(session)
    subjects = await list_subjects(session)
    await message.answer("📚 Choose a subject:", reply_markup=subjects_kb(subjects))


@router.message(Command("subjects"))
async def subjects_command(message: Message, session):
    await ensure_subjects(session)
    subjects = await list_subjects(session)
    await message.answer("📚 Choose a subject:", reply_markup=subjects_kb(subjects))


async def show_subjects(cb_or_msg, session, page: int = 0):
    subjects = await list_subjects(session)
    kb = subjects_kb(subjects, page=page)
    text = "📚 Choose a subject:"
    if isinstance(cb_or_msg, Message):
        await cb_or_msg.answer(text, reply_markup=kb)
    else:
        await cb_or_msg.message.edit_text(text, reply_markup=kb)


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
        groups = await list_subject_teacher_groups(session, subject.id)
        d, t = await subject_progress(session, db_user.id, subject.id)
        text = _teacher_groups_text(subject, d, t, groups)
        kb = teachers_kb(
            groups, subject.id, page=0, per_page=TEACHERS_PER_PAGE
        )
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
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
        _, subject_raw, teacher_raw = cb.data.split(":")
        subject = await session.get(Subject, int(subject_raw))
        if not subject:
            await cb.answer("Not found", show_alert=True)
            return
        shown = await _show_teacher_lectures(
            cb, session, subject, int(teacher_raw), 0
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
        d, t = await subject_progress(session, db_user.id, subject.id)
        text = _subject_text(subject, d, t, len(lectures))
        kb = lectures_kb(lectures, subject.slug, page=int(page))
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        await _answer_subject_error(cb, f"Failed to change lecture page for {cb.data}")
        return
    await cb.answer()
