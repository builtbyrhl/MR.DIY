from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func, update, delete, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    AdminUser,
    Bookmark,
    Lecture,
    Payment,
    Progress,
    Subject,
    Teacher,
    TeacherAlias,
    Topic,
    UploadItem,
    User,
    lecture_topics,
    upload_item_topics,
)
from bot.services.lecture_display import decorate_lecture_titles

TEACHER_UNSPECIFIED = "Teacher not specified"


@dataclass(frozen=True)
class TeacherGroup:
    teacher_id: Optional[int]
    name: str
    lecture_count: int


@dataclass(frozen=True)
class TopicGroup:
    topic_id: int
    name: str
    lecture_count: int


# ---------- Users ----------
async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    from bot.services.auth import is_admin as admin_check

    is_admin_user = admin_check(tg_user.id)
    user = await session.get(User, tg_user.id)
    if user is None:
        user = User(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            is_admin=is_admin_user,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        user.username = tg_user.username
        user.full_name = tg_user.full_name
        user.is_admin = is_admin_user
        user.last_active = datetime.utcnow()
        await session.commit()
    return user


# ---------- Admins ----------
async def list_db_admins(session: AsyncSession) -> list[AdminUser]:
    res = await session.execute(select(AdminUser).order_by(AdminUser.user_id))
    return list(res.scalars().all())


async def add_db_admin(
    session: AsyncSession, user_id: int, granted_by: int
) -> tuple[Optional[AdminUser], Optional[str]]:
    if user_id <= 0:
        return None, "Telegram user ID must be a positive number."
    user = await session.get(User, user_id)
    if user is None:
        return None, (
            f"User {user_id} was not found. They must start the bot first."
        )
    existing = await session.get(AdminUser, user_id)
    if existing is not None:
        return existing, "User is already a database admin."
    admin = AdminUser(user_id=user_id, granted_by=granted_by)
    session.add(admin)
    user.is_admin = True
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None, "User is already a database admin."
    await session.refresh(admin)
    return admin, None


async def remove_db_admin(
    session: AsyncSession, user_id: int
) -> tuple[Optional[AdminUser], Optional[str]]:
    admin = await session.get(AdminUser, user_id)
    if admin is None:
        return None, "User is not a database-granted admin."
    user = await session.get(User, user_id)
    await session.delete(admin)
    if user is not None:
        from bot.services.auth import is_owner

        user.is_admin = is_owner(user_id)
    await session.commit()
    return admin, None


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


async def list_subject_lecture_counts(
    session: AsyncSession,
) -> dict[int, int]:
    res = await session.execute(
        select(Lecture.subject_id, func.count(Lecture.id)).group_by(
            Lecture.subject_id
        )
    )
    return {int(subject_id): int(count) for subject_id, count in res.all()}


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


async def find_matching_teachers(
    session: AsyncSession, subject_id: int, name: str
) -> list[Teacher]:
    """Return subject-scoped teachers matching a canonical name or alias.

    Spelling variants are never guessed. Administrators add explicit aliases
    such as ``Dr Mohammad Azam`` for a teacher displayed as
    ``Dr. Muhammad Azam``.
    """
    normalized = normalize_teacher_name(name)
    if not normalized:
        return []

    canonical = await session.execute(
        select(Teacher).where(
            Teacher.subject_id == subject_id,
            Teacher.normalized_name == normalized,
        )
    )
    aliased = await session.execute(
        select(Teacher)
        .join(TeacherAlias, TeacherAlias.teacher_id == Teacher.id)
        .where(
            Teacher.subject_id == subject_id,
            TeacherAlias.subject_id == subject_id,
            TeacherAlias.normalized_name == normalized,
        )
    )
    teachers = {teacher.id: teacher for teacher in canonical.scalars().all()}
    teachers.update({teacher.id: teacher for teacher in aliased.scalars().all()})
    return [teachers[teacher_id] for teacher_id in sorted(teachers)]


def _clean_teacher_name(name: str) -> str:
    return " ".join(name.split())


def _validate_teacher_name(
    name: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    clean_name = _clean_teacher_name(name)
    if not clean_name:
        return None, None, "Teacher name is required."
    if len(clean_name) > 128:
        return None, None, "Teacher name must be 128 characters or fewer."
    normalized = normalize_teacher_name(clean_name)
    if not normalized:
        return None, None, "Teacher name is required."
    return clean_name, normalized, None


async def _canonical_teacher_conflict(
    session: AsyncSession,
    subject_id: int,
    normalized: str,
    *,
    exclude_teacher_id: Optional[int] = None,
) -> Optional[Teacher]:
    query = select(Teacher).where(
        Teacher.subject_id == subject_id,
        Teacher.normalized_name == normalized,
    )
    if exclude_teacher_id is not None:
        query = query.where(Teacher.id != exclude_teacher_id)
    return await session.scalar(query)


async def _alias_conflict(
    session: AsyncSession,
    subject_id: int,
    normalized: str,
    *,
    exclude_teacher_id: Optional[int] = None,
) -> Optional[TeacherAlias]:
    query = select(TeacherAlias).where(
        TeacherAlias.subject_id == subject_id,
        TeacherAlias.normalized_name == normalized,
    )
    if exclude_teacher_id is not None:
        query = query.where(TeacherAlias.teacher_id != exclude_teacher_id)
    return await session.scalar(query)


async def list_teacher_aliases(
    session: AsyncSession, teacher_id: int
) -> list[TeacherAlias]:
    res = await session.execute(
        select(TeacherAlias)
        .where(TeacherAlias.teacher_id == teacher_id)
        .order_by(func.lower(TeacherAlias.name), TeacherAlias.id)
    )
    return list(res.scalars().all())


def _clean_aliases(aliases: Optional[list[str]]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for alias in aliases or []:
        clean = _clean_teacher_name(alias)
        if not clean:
            continue
        normalized = normalize_teacher_name(clean)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(clean)
    return cleaned


async def add_teacher(
    session: AsyncSession,
    subject_id: int,
    name: str,
    aliases: Optional[list[str]] = None,
) -> tuple[Optional[Teacher], Optional[str]]:
    subject = await session.get(Subject, subject_id)
    if subject is None:
        return None, "Subject not found."

    clean_name, normalized, error = _validate_teacher_name(name)
    if error:
        return None, error
    assert clean_name is not None and normalized is not None

    existing = await _canonical_teacher_conflict(
        session, subject_id, normalized
    )
    if existing is not None:
        return None, f"Teacher already exists: #{existing.id} {existing.name}."

    clean_aliases = _clean_aliases(aliases)
    if any(len(alias) > 128 for alias in clean_aliases):
        return None, "Teacher aliases must be 128 characters or fewer."
    for alias in clean_aliases:
        alias_norm = normalize_teacher_name(alias)
        if alias_norm == normalized:
            continue
        canonical_conflict = await _canonical_teacher_conflict(
            session, subject_id, alias_norm
        )
        if canonical_conflict is not None:
            return (
                None,
                f"Alias '{alias}' is already the name of teacher "
                f"#{canonical_conflict.id} {canonical_conflict.name}.",
            )
        alias_conflict = await _alias_conflict(
            session, subject_id, alias_norm
        )
        if alias_conflict is not None:
            return None, f"Alias '{alias}' is already registered."

    teacher = Teacher(
        subject_id=subject_id,
        name=clean_name,
        normalized_name=normalized,
    )
    session.add(teacher)
    try:
        await session.flush()
        for alias in clean_aliases:
            alias_norm = normalize_teacher_name(alias)
            if alias_norm == normalized:
                continue
            session.add(
                TeacherAlias(
                    teacher_id=teacher.id,
                    subject_id=subject_id,
                    name=alias,
                    normalized_name=alias_norm,
                )
            )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None, "Teacher or alias already exists for this subject."
    await session.refresh(teacher)
    return teacher, None


async def add_teacher_alias(
    session: AsyncSession, teacher_id: int, alias: str
) -> tuple[Optional[TeacherAlias], Optional[str]]:
    teacher = await session.get(Teacher, teacher_id)
    if teacher is None:
        return None, "Teacher not found."

    clean_alias, normalized, error = _validate_teacher_name(alias)
    if error:
        return None, error.replace("Teacher name", "Alias")
    assert clean_alias is not None and normalized is not None

    if normalized == teacher.normalized_name:
        return None, "Alias is the same as the teacher's display name."
    canonical_conflict = await _canonical_teacher_conflict(
        session, teacher.subject_id, normalized
    )
    if canonical_conflict is not None:
        return (
            None,
            f"Alias is already the name of teacher "
            f"#{canonical_conflict.id} {canonical_conflict.name}.",
        )
    alias_conflict = await _alias_conflict(
        session, teacher.subject_id, normalized
    )
    if alias_conflict is not None:
        if alias_conflict.teacher_id == teacher.id:
            return None, "Alias already exists for this teacher."
        return (
            None,
            f"Alias already belongs to teacher #{alias_conflict.teacher_id}.",
        )

    record = TeacherAlias(
        teacher_id=teacher.id,
        subject_id=teacher.subject_id,
        name=clean_alias,
        normalized_name=normalized,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None, "Alias already exists for this subject."
    await session.refresh(record)
    return record, None


async def remove_teacher_alias(
    session: AsyncSession, teacher_id: int, alias: str
) -> tuple[bool, Optional[str]]:
    normalized = normalize_teacher_name(alias)
    if not normalized:
        return False, "Alias is required."
    result = await session.execute(
        delete(TeacherAlias).where(
            TeacherAlias.teacher_id == teacher_id,
            TeacherAlias.normalized_name == normalized,
        )
    )
    await session.commit()
    if result.rowcount != 1:
        return False, "Alias not found for this teacher."
    return True, None


async def rename_teacher(
    session: AsyncSession, teacher_id: int, new_name: str
) -> tuple[Optional[Teacher], Optional[str]]:
    teacher = await session.get(Teacher, teacher_id)
    if teacher is None:
        return None, "Teacher not found."

    clean_name, normalized, error = _validate_teacher_name(new_name)
    if error:
        return None, error
    assert clean_name is not None and normalized is not None

    canonical_conflict = await _canonical_teacher_conflict(
        session,
        teacher.subject_id,
        normalized,
        exclude_teacher_id=teacher.id,
    )
    if canonical_conflict is not None:
        return (
            None,
            f"Teacher already exists: #{canonical_conflict.id} "
            f"{canonical_conflict.name}.",
        )
    alias_conflict = await _alias_conflict(
        session, teacher.subject_id, normalized
    )
    if alias_conflict is not None:
        return (
            None,
            f"An alias for teacher #{alias_conflict.teacher_id} already uses "
            "that name.",
        )

    teacher.name = clean_name
    teacher.normalized_name = normalized
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None, "Teacher already exists for this subject."
    await session.refresh(teacher)
    return teacher, None


# ---------- Topics ----------
def normalize_topic_name(name: str) -> str:
    """Normalize spelling/punctuation for subject-scoped topic matching."""
    without_punctuation = re.sub(r"[^\w\s]", " ", name.casefold())
    return " ".join(without_punctuation.split())


async def get_topic(session: AsyncSession, topic_id: int) -> Optional[Topic]:
    return await session.get(Topic, topic_id)


async def list_topics(session: AsyncSession, subject_id: int) -> list[Topic]:
    res = await session.execute(
        select(Topic)
        .where(Topic.subject_id == subject_id)
        .order_by(func.lower(Topic.name), Topic.id)
    )
    return list(res.scalars().all())


async def find_matching_topics(
    session: AsyncSession, subject_id: int, name: str
) -> list[Topic]:
    """Return topics in one subject whose normalized names match exactly."""
    normalized = normalize_topic_name(name)
    if not normalized:
        return []
    res = await session.execute(
        select(Topic)
        .where(
            Topic.subject_id == subject_id,
            Topic.normalized_name == normalized,
        )
        .order_by(Topic.id)
    )
    return list(res.scalars().all())


async def add_topic(
    session: AsyncSession, subject_id: int, name: str
) -> tuple[Optional[Topic], Optional[str]]:
    subject = await session.get(Subject, subject_id)
    if subject is None:
        return None, "Subject not found."

    clean_name = " ".join(name.split())
    if not clean_name:
        return None, "Topic name is required."
    if len(clean_name) > 128:
        return None, "Topic name must be 128 characters or fewer."
    normalized = normalize_topic_name(clean_name)
    if not normalized:
        return None, "Topic name is required."

    existing = await session.scalar(
        select(Topic).where(
            Topic.subject_id == subject_id,
            Topic.normalized_name == normalized,
        )
    )
    if existing is not None:
        return None, f"Topic already exists: #{existing.id} {existing.name}."

    topic = Topic(
        subject_id=subject_id,
        name=clean_name,
        normalized_name=normalized,
    )
    session.add(topic)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None, "Topic already exists for this subject."
    await session.refresh(topic)
    return topic, None


def _clean_topic_ids(topic_ids: list[int]) -> list[int]:
    cleaned: list[int] = []
    for topic_id in topic_ids:
        if topic_id <= 0:
            continue
        if topic_id not in cleaned:
            cleaned.append(topic_id)
    return cleaned


async def list_item_topics(
    session: AsyncSession, item_id: int
) -> list[Topic]:
    res = await session.execute(
        select(Topic)
        .join(
            upload_item_topics,
            upload_item_topics.c.topic_id == Topic.id,
        )
        .where(upload_item_topics.c.upload_item_id == item_id)
        .order_by(func.lower(Topic.name), Topic.id)
    )
    return list(res.scalars().all())


async def list_lecture_topics(
    session: AsyncSession, lecture_id: int
) -> list[Topic]:
    res = await session.execute(
        select(Topic)
        .join(
            lecture_topics,
            lecture_topics.c.topic_id == Topic.id,
        )
        .where(lecture_topics.c.lecture_id == lecture_id)
        .order_by(func.lower(Topic.name), Topic.id)
    )
    return list(res.scalars().all())


async def replace_item_topics(
    session: AsyncSession, item_id: int, topic_ids: list[int]
) -> None:
    await session.execute(
        delete(upload_item_topics).where(
            upload_item_topics.c.upload_item_id == item_id
        )
    )
    cleaned = _clean_topic_ids(topic_ids)
    if cleaned:
        await session.execute(
            insert(upload_item_topics),
            [
                {"upload_item_id": item_id, "topic_id": topic_id}
                for topic_id in cleaned
            ],
        )


async def replace_lecture_topics(
    session: AsyncSession, lecture_id: int, topic_ids: list[int]
) -> None:
    await session.execute(
        delete(lecture_topics).where(lecture_topics.c.lecture_id == lecture_id)
    )
    cleaned = _clean_topic_ids(topic_ids)
    if cleaned:
        await session.execute(
            insert(lecture_topics),
            [
                {"lecture_id": lecture_id, "topic_id": topic_id}
                for topic_id in cleaned
            ],
        )


async def sync_item_topics_to_lecture(
    session: AsyncSession, item_id: int, lecture_id: int
) -> None:
    topics = await list_item_topics(session, item_id)
    await replace_lecture_topics(
        session, lecture_id, [topic.id for topic in topics]
    )


async def list_subject_topic_groups(
    session: AsyncSession, subject_id: int
) -> list[TopicGroup]:
    """Return topics with published lectures in one subject."""
    res = await session.execute(
        select(
            Topic.id,
            Topic.name,
            func.count(func.distinct(Lecture.id)),
        )
        .join(lecture_topics, lecture_topics.c.topic_id == Topic.id)
        .join(Lecture, Lecture.id == lecture_topics.c.lecture_id)
        .where(
            Topic.subject_id == subject_id,
            Lecture.subject_id == subject_id,
        )
        .group_by(Topic.id, Topic.name)
        .order_by(func.lower(Topic.name), Topic.id)
    )
    return [
        TopicGroup(topic_id=row[0], name=row[1], lecture_count=int(row[2]))
        for row in res.all()
    ]


async def list_teacher_topic_groups(
    session: AsyncSession,
    subject_id: int,
    teacher_id: Optional[int],
) -> list[TopicGroup]:
    """Return topics with lectures by one teacher, including unspecified."""
    teacher_filter = (
        Lecture.teacher_id.is_(None)
        if teacher_id is None
        else Lecture.teacher_id == teacher_id
    )
    res = await session.execute(
        select(
            Topic.id,
            Topic.name,
            func.count(func.distinct(Lecture.id)),
        )
        .join(lecture_topics, lecture_topics.c.topic_id == Topic.id)
        .join(Lecture, Lecture.id == lecture_topics.c.lecture_id)
        .where(
            Topic.subject_id == subject_id,
            Lecture.subject_id == subject_id,
            teacher_filter,
        )
        .group_by(Topic.id, Topic.name)
        .order_by(func.lower(Topic.name), Topic.id)
    )
    return [
        TopicGroup(topic_id=row[0], name=row[1], lecture_count=int(row[2]))
        for row in res.all()
    ]


async def list_lectures_by_teacher_topic(
    session: AsyncSession,
    subject_id: int,
    teacher_id: Optional[int],
    topic_id: int,
    include_premium: bool = True,
) -> list[Lecture]:
    teacher_filter = (
        Lecture.teacher_id.is_(None)
        if teacher_id is None
        else Lecture.teacher_id == teacher_id
    )
    q = (
        select(Lecture)
        .join(
            lecture_topics,
            lecture_topics.c.lecture_id == Lecture.id,
        )
        .where(
            Lecture.subject_id == subject_id,
            teacher_filter,
            lecture_topics.c.topic_id == topic_id,
        )
        .order_by(Lecture.order, Lecture.channel_message_id, Lecture.id)
    )
    if not include_premium:
        q = q.where(Lecture.is_premium.is_(False))
    res = await session.execute(q)
    lectures = list(res.scalars().all())
    await decorate_lecture_titles(session, lectures)
    return lectures


async def list_lectures_by_topic(
    session: AsyncSession,
    subject_id: int,
    topic_id: int,
    include_premium: bool = True,
) -> list[Lecture]:
    q = (
        select(Lecture)
        .join(
            lecture_topics,
            lecture_topics.c.lecture_id == Lecture.id,
        )
        .where(
            Lecture.subject_id == subject_id,
            lecture_topics.c.topic_id == topic_id,
        )
        .order_by(Lecture.order, Lecture.channel_message_id, Lecture.id)
    )
    if not include_premium:
        q = q.where(Lecture.is_premium.is_(False))
    res = await session.execute(q)
    lectures = list(res.scalars().all())
    await decorate_lecture_titles(session, lectures)
    return lectures


async def set_lecture_topics(
    session: AsyncSession, lecture_id: int, topic_ids: list[int]
) -> tuple[Optional[Lecture], Optional[str]]:
    """Correct a lecture's topics and mirror them to its linked upload.

    Updating both records keeps startup projection from restoring stale topic
    metadata. Legacy lectures without a source upload can be classified too.
    """
    lecture = await session.get(Lecture, lecture_id)
    if lecture is None:
        return None, "Lecture not found."

    topics: list[Topic] = []
    for topic_id in _clean_topic_ids(topic_ids):
        topic = await session.get(Topic, topic_id)
        if topic is None:
            return None, f"Topic #{topic_id} not found."
        if topic.subject_id != lecture.subject_id:
            return None, "Topic belongs to a different subject."
        topics.append(topic)

    await replace_lecture_topics(
        session, lecture.id, [topic.id for topic in topics]
    )
    if lecture.source_upload_item_id is not None:
        item = await session.get(UploadItem, lecture.source_upload_item_id)
        if item is not None:
            from bot.db.ingestion_repo import _recompute_item_status

            await replace_item_topics(
                session, item.id, [topic.id for topic in topics]
            )
            item.topic_source = "manual"
            item.topic_review_reason = None
            if item.status in {"classified", "needs_review"}:
                _recompute_item_status(item)
    await session.commit()
    await session.refresh(lecture)
    return lecture, None


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
    lectures = list(res.scalars().all())
    await decorate_lecture_titles(session, lectures)
    return lectures


async def set_lecture_title(
    session: AsyncSession, lecture_id: int, title: str
) -> tuple[Optional[Lecture], Optional[str]]:
    """Correct a student-visible lecture title without touching the media.

    The linked upload title is mirrored so startup catalogue projection cannot
    restore a stale title. The original caption remains unchanged for audit.
    """
    lecture = await session.get(Lecture, lecture_id)
    if lecture is None:
        return None, "Lecture not found."

    clean_title = " ".join((title or "").split())
    if not clean_title:
        return None, "Lecture title is required."
    if len(clean_title) > 256:
        return None, "Lecture title must be 256 characters or fewer."

    lecture.title = clean_title
    if lecture.source_upload_item_id is not None:
        item = await session.get(UploadItem, lecture.source_upload_item_id)
        if item is not None:
            item.title = clean_title
    await session.commit()
    await session.refresh(lecture)
    return lecture, None


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
            from bot.db.ingestion_repo import _recompute_item_status

            item.teacher_id = teacher_id
            item.teacher_source = "manual"
            item.teacher_review_reason = None
            if item.status in {"classified", "needs_review"}:
                _recompute_item_status(item)
    await session.commit()
    await session.refresh(lecture)
    return lecture, None


# ---------- Lectures ----------
async def list_lectures(session: AsyncSession, subject_id: int, include_premium: bool = True) -> list[Lecture]:
    q = select(Lecture).where(Lecture.subject_id == subject_id).order_by(Lecture.order, Lecture.channel_message_id, Lecture.id)
    if not include_premium:
        q = q.where(Lecture.is_premium.is_(False))
    res = await session.execute(q)
    lectures = list(res.scalars().all())
    await decorate_lecture_titles(session, lectures)
    return lectures


async def get_lecture(session: AsyncSession, lecture_id: int) -> Optional[Lecture]:
    return await session.get(Lecture, lecture_id)


async def search_lectures(session: AsyncSession, query: str, limit: int = 20) -> list[Lecture]:
    q = f"%{query.lower()}%"
    res = await session.execute(
        select(Lecture).where(
            (Lecture.title.ilike(q)) | (Lecture.tags.ilike(q)) | (Lecture.description.ilike(q))
        ).order_by(Lecture.id.desc()).limit(limit)
    )
    lectures = list(res.scalars().all())
    await decorate_lecture_titles(session, lectures)
    return lectures


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


async def list_watched_lecture_ids(
    session: AsyncSession, user_id: int
) -> set[int]:
    res = await session.execute(
        select(Progress.lecture_id).where(
            Progress.user_id == user_id,
            Progress.watched.is_(True),
        )
    )
    return {int(lecture_id) for lecture_id in res.scalars().all()}


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
