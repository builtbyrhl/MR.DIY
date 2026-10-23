from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Bookmark,
    Lecture,
    Payment,
    Progress,
    Subject,
    Teacher,
    UploadItem,
    User,
)

TEACHER_UNSPECIFIED = "Teacher not specified"


@dataclass(frozen=True)
class TeacherGroup:
    teacher_id: Optional[int]
    name: str
    lecture_count: int


# ---------- Users ----------
async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    user = await session.get(User, tg_user.id)
    if user is None:
        user = User(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            is_admin=tg_user.id in __import__("bot.config", fromlist=["get_settings"]).get_settings().admin_ids,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        user.username = tg_user.username
        user.full_name = tg_user.full_name
        user.last_active = datetime.utcnow()
        await session.commit()
    return user


async def is_premium(user: User) -> bool:
    if user.is_premium and (user.premium_until is None or user.premium_until > datetime.utcnow()):
        return True
    return False


async def grant_premium(session: AsyncSession, user_id: int, days: int = 365):
    user = await session.get(User, user_id)
    if not user:
        return
    base = user.premium_until if (user.premium_until and user.premium_until > datetime.utcnow()) else datetime.utcnow()
    user.premium_until = base + timedelta(days=days)
    user.is_premium = True
    await session.commit()


# ---------- Subjects ----------
async def list_subjects(session: AsyncSession) -> list[Subject]:
    res = await session.execute(select(Subject).order_by(Subject.order, Subject.name))
    return list(res.scalars().all())


async def get_subject(session: AsyncSession, slug: str) -> Optional[Subject]:
    res = await session.execute(select(Subject).where(Subject.slug == slug))
    return res.scalar_one_or_none()


# ---------- Teachers ----------
def normalize_teacher_name(name: str) -> str:
    """Normalize spelling/punctuation for subject-scoped duplicate checks."""
    without_punctuation = re.sub(r"[^\w\s]", " ", name.casefold())
    return " ".join(without_punctuation.split())


async def get_teacher(session: AsyncSession, teacher_id: int) -> Optional[Teacher]:
    return await session.get(Teacher, teacher_id)


async def list_teachers(session: AsyncSession, subject_id: int) -> list[Teacher]:
    res = await session.execute(
        select(Teacher)
        .where(Teacher.subject_id == subject_id)
        .order_by(func.lower(Teacher.name), Teacher.id)
    )
    return list(res.scalars().all())


async def add_teacher(
    session: AsyncSession, subject_id: int, name: str
) -> tuple[Optional[Teacher], Optional[str]]:
    subject = await session.get(Subject, subject_id)
    if subject is None:
        return None, "Subject not found."

    clean_name = " ".join(name.split())
    if not clean_name:
        return None, "Teacher name is required."
    if len(clean_name) > 128:
        return None, "Teacher name must be 128 characters or fewer."
    normalized = normalize_teacher_name(clean_name)
    if not normalized:
        return None, "Teacher name is required."

    existing = await session.scalar(
        select(Teacher).where(
            Teacher.subject_id == subject_id,
            Teacher.normalized_name == normalized,
        )
    )
    if existing is not None:
        return None, f"Teacher already exists: #{existing.id} {existing.name}."

    teacher = Teacher(
        subject_id=subject_id,
        name=clean_name,
        normalized_name=normalized,
    )
    session.add(teacher)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None, "Teacher already exists for this subject."
    await session.refresh(teacher)
    return teacher, None


async def list_subject_teacher_groups(
    session: AsyncSession, subject_id: int
) -> list[TeacherGroup]:
    """Return teachers with published lectures, plus the unspecified bucket."""
    res = await session.execute(
        select(Teacher.id, Teacher.name, func.count(Lecture.id))
        .join(Lecture, Lecture.teacher_id == Teacher.id)
        .where(
            Teacher.subject_id == subject_id,
            Lecture.subject_id == subject_id,
        )
        .group_by(Teacher.id, Teacher.name)
        .order_by(func.lower(Teacher.name), Teacher.id)
    )
    groups = [
        TeacherGroup(teacher_id=row[0], name=row[1], lecture_count=int(row[2]))
        for row in res.all()
    ]
    unspecified = await session.scalar(
        select(func.count(Lecture.id)).where(
            Lecture.subject_id == subject_id,
            Lecture.teacher_id.is_(None),
        )
    )
    if unspecified:
        groups.append(
            TeacherGroup(
                teacher_id=None,
                name=TEACHER_UNSPECIFIED,
                lecture_count=int(unspecified),
            )
        )
    return groups


async def list_lectures_by_teacher(
    session: AsyncSession,
    subject_id: int,
    teacher_id: Optional[int],
    include_premium: bool = True,
) -> list[Lecture]:
    q = select(Lecture).where(Lecture.subject_id == subject_id)
    if teacher_id is None:
        q = q.where(Lecture.teacher_id.is_(None))
    else:
        q = q.where(Lecture.teacher_id == teacher_id)
    q = q.order_by(Lecture.order, Lecture.channel_message_id, Lecture.id)
    if not include_premium:
        q = q.where(Lecture.is_premium.is_(False))
    res = await session.execute(q)
    return list(res.scalars().all())


async def set_lecture_teacher(
    session: AsyncSession, lecture_id: int, teacher_id: Optional[int]
) -> tuple[Optional[Lecture], Optional[str]]:
    """Correct a lecture's teacher and keep the linked upload in sync.

    Updating both rows prevents startup projection from restoring stale
    teacher metadata on the next restart.
    """
    lecture = await session.get(Lecture, lecture_id)
    if lecture is None:
        return None, "Lecture not found."

    if teacher_id is not None:
        teacher = await session.get(Teacher, teacher_id)
        if teacher is None:
            return None, "Teacher not found."
        if teacher.subject_id != lecture.subject_id:
            return None, "Teacher belongs to a different subject."

    lecture.teacher_id = teacher_id
    if lecture.source_upload_item_id is not None:
        item = await session.get(UploadItem, lecture.source_upload_item_id)
        if item is not None:
            item.teacher_id = teacher_id
    await session.commit()
    await session.refresh(lecture)
    return lecture, None


# ---------- Lectures ----------
async def list_lectures(session: AsyncSession, subject_id: int, include_premium: bool = True) -> list[Lecture]:
    q = select(Lecture).where(Lecture.subject_id == subject_id).order_by(Lecture.order, Lecture.channel_message_id, Lecture.id)
    if not include_premium:
        q = q.where(Lecture.is_premium.is_(False))
    res = await session.execute(q)
    return list(res.scalars().all())


async def get_lecture(session: AsyncSession, lecture_id: int) -> Optional[Lecture]:
    return await session.get(Lecture, lecture_id)


async def search_lectures(session: AsyncSession, query: str, limit: int = 20) -> list[Lecture]:
    q = f"%{query.lower()}%"
    res = await session.execute(
        select(Lecture).where(
            (Lecture.title.ilike(q)) | (Lecture.tags.ilike(q)) | (Lecture.description.ilike(q))
        ).order_by(Lecture.id.desc()).limit(limit)
    )
    return list(res.scalars().all())


# ---------- Progress ----------
async def mark_watched(session: AsyncSession, user_id: int, lecture_id: int):
    prog = await session.execute(
        select(Progress).where(Progress.user_id == user_id, Progress.lecture_id == lecture_id)
    )
    p = prog.scalar_one_or_none()
    if p is None:
        p = Progress(user_id=user_id, lecture_id=lecture_id, watched=True, watched_at=datetime.utcnow())
        session.add(p)
    else:
        p.watched = True
        p.watched_at = datetime.utcnow()
    await session.commit()


async def is_watched(session: AsyncSession, user_id: int, lecture_id: int) -> bool:
    res = await session.execute(
        select(Progress.watched).where(Progress.user_id == user_id, Progress.lecture_id == lecture_id)
    )
    v = res.scalar_one_or_none()
    return bool(v)


async def subject_progress(session: AsyncSession, user_id: int, subject_id: int) -> tuple[int, int]:
    total = await session.execute(
        select(func.count(Lecture.id)).where(Lecture.subject_id == subject_id)
    )
    t = total.scalar() or 0
    done = await session.execute(
        select(func.count(Progress.id)).join(Lecture, Lecture.id == Progress.lecture_id)
        .where(Progress.user_id == user_id, Lecture.subject_id == subject_id, Progress.watched.is_(True))
    )
    d = done.scalar() or 0
    return d, t


async def last_watched_lecture(session: AsyncSession, user_id: int) -> Optional[Lecture]:
    res = await session.execute(
        select(Lecture).join(Progress, Progress.lecture_id == Lecture.id)
        .where(Progress.user_id == user_id, Progress.watched.is_(True))
        .order_by(Progress.watched_at.desc()).limit(1)
    )
    return res.scalar_one_or_none()


# ---------- Bookmarks ----------
async def add_bookmark(session: AsyncSession, user_id: int, lecture_id: int) -> bool:
    try:
        session.add(Bookmark(user_id=user_id, lecture_id=lecture_id))
        await session.commit()
        return True
    except Exception:
        await session.rollback()
        return False


async def remove_bookmark(session: AsyncSession, user_id: int, lecture_id: int):
    await session.execute(
        delete(Bookmark).where(Bookmark.user_id == user_id, Bookmark.lecture_id == lecture_id)
    )
    await session.commit()


async def list_bookmarks(session: AsyncSession, user_id: int) -> list[Lecture]:
    res = await session.execute(
        select(Lecture).join(Bookmark, Bookmark.lecture_id == Lecture.id)
        .where(Bookmark.user_id == user_id).order_by(Bookmark.created_at.desc())
    )
    return list(res.scalars().all())


# ---------- Payments ----------
async def create_payment(session: AsyncSession, user_id: int, amount: int, order_id: str) -> Payment:
    p = Payment(user_id=user_id, amount_inr=amount, provider_order_id=order_id)
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


async def mark_payment_paid(session: AsyncSession, order_id: str, payment_id: str):
    res = await session.execute(select(Payment).where(Payment.provider_order_id == order_id))
    p = res.scalar_one_or_none()
    if p:
        p.status = "paid"
        p.provider_payment_id = payment_id
        p.paid_at = datetime.utcnow()
        await session.commit()
        await grant_premium(session, p.user_id, 365)
