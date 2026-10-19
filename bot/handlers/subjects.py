from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command

from bot.db.repo import list_subjects, get_subject, list_lectures, subject_progress, is_premium
from bot.keyboards.inline import lectures_kb, subjects_kb
from bot.services.catalog import ensure_subjects

router = Router()


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


@router.callback_query(F.data == "subjects")
async def cb_subjects(cb: CallbackQuery, session):
    await show_subjects(cb, session)
    await cb.answer()


@router.callback_query(F.data.startswith("subjpage:"))
async def cb_subjpage(cb: CallbackQuery, session):
    page = int(cb.data.split(":")[1])
    await show_subjects(cb, session, page=page)
    await cb.answer()


@router.callback_query(F.data.startswith("subj:"))
async def open_subject(cb: CallbackQuery, session, db_user):
    slug = cb.data.split(":", 1)[1]
    subject = await get_subject(session, slug)
    if not subject:
        await cb.answer("Not found", show_alert=True)
        return
    premium = await is_premium(db_user)
    lectures = await list_lectures(session, subject.id, include_premium=True)
    # store current subject in cb message? use data
    d, t = await subject_progress(session, db_user.id, subject.id)
    pct = int(d / t * 100) if t else 0
    text = f"{subject.emoji} *{subject.name}*\nProgress: {d}/{t} ({pct}%)"
    kb = lectures_kb(lectures, subject.slug, page=0)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("lecpage:"))
async def lec_page(cb: CallbackQuery, session, db_user):
    _, slug, page = cb.data.split(":")
    subject = await get_subject(session, slug)
    lectures = await list_lectures(session, subject.id, include_premium=True)
    d, t = await subject_progress(session, db_user.id, subject.id)
    pct = int(d / t * 100) if t else 0
    text = f"{subject.emoji} *{subject.name}*\nProgress: {d}/{t} ({pct}%)"
    kb = lectures_kb(lectures, subject.slug, page=int(page))
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await cb.answer()
