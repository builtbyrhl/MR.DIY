from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command

from bot.db.repo import list_subjects, get_subject, list_lectures, subject_progress
from bot.keyboards.inline import lectures_kb, subjects_kb
from bot.services.catalog import ensure_subjects

router = Router()
logger = logging.getLogger(__name__)


def _subject_text(subject, done: int, total: int, lecture_count: int) -> str:
    pct = int(done / total * 100) if total else 0
    text = f"{subject.emoji} *{subject.name}*\nProgress: {done}/{total} ({pct}%)"
    if lecture_count == 0:
        text += "\n\nNo lectures uploaded yet for this subject."
    return text


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
        lectures = await list_lectures(session, subject.id, include_premium=True)
        d, t = await subject_progress(session, db_user.id, subject.id)
        text = _subject_text(subject, d, t, len(lectures))
        kb = lectures_kb(lectures, subject.slug, page=0)
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception:
        await _answer_subject_error(cb, f"Failed to open subject {slug}")
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
