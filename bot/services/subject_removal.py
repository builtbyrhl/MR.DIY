from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Course,
    Lecture,
    PinnedSubject,
    Subject,
    SubjectFollower,
    Teacher,
    TeacherAlias,
    Topic,
)
from bot.db.repo import record_admin_action


@dataclass(frozen=True)
class SubjectReference:
    label: str
    count: int
    samples: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubjectRemovalPreview:
    subject_id: int
    name: str
    slug: str
    references: tuple[SubjectReference, ...]

    @property
    def has_dependencies(self) -> bool:
        return any(reference.count > 0 for reference in self.references)

    def reference_lines(self) -> list[str]:
        lines = []
        for reference in self.references:
            if reference.count <= 0:
                continue
            line = f"{reference.label}: {reference.count}"
            if reference.samples:
                line += f" ({', '.join(reference.samples)})"
            lines.append(line)
        return lines or ["Linked records: none"]


_SAMPLE_REFERENCES = (
    ("Courses", Course, Course.name),
    ("Lectures", Lecture, Lecture.title),
    ("Teachers", Teacher, Teacher.name),
    ("Teacher aliases", TeacherAlias, TeacherAlias.name),
    ("Topics", Topic, Topic.name),
)

_COUNT_ONLY_REFERENCES = (
    ("Subject followers", SubjectFollower),
    ("Pinned subjects", PinnedSubject),
)

_SUMMARY_MODELS = (
    ("courses", Course),
    ("lectures", Lecture),
    ("teachers", Teacher),
    ("teacher_aliases", TeacherAlias),
    ("topics", Topic),
    ("followers", SubjectFollower),
    ("pins", PinnedSubject),
)


@dataclass(frozen=True)
class SubjectDependencySummary:
    courses: int = 0
    lectures: int = 0
    teachers: int = 0
    teacher_aliases: int = 0
    topics: int = 0
    followers: int = 0
    pins: int = 0

    @property
    def total(self) -> int:
        return (
            self.courses
            + self.lectures
            + self.teachers
            + self.teacher_aliases
            + self.topics
            + self.followers
            + self.pins
        )

    @property
    def has_dependencies(self) -> bool:
        return self.total > 0

    def counts_text(self) -> str:
        counts = (
            ("courses", self.courses),
            ("lectures", self.lectures),
            ("teachers", self.teachers),
            ("aliases", self.teacher_aliases),
            ("topics", self.topics),
            ("followers", self.followers),
            ("pins", self.pins),
        )
        parts = [f"{label}={count}" for label, count in counts if count]
        return ", ".join(parts) if parts else "none"


def _clip(value: object, limit: int = 60) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return f"{text[: limit - 3]}..."
    return text


async def _reference_count(
    session: AsyncSession, model, subject_id: int
) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(model)
        .where(model.subject_id == subject_id)
    )
    return int(count or 0)


async def _reference_samples(
    session: AsyncSession, model, label_column, subject_id: int
) -> tuple[str, ...]:
    result = await session.execute(
        select(model.id, label_column)
        .where(model.subject_id == subject_id)
        .order_by(model.id)
        .limit(5)
    )
    return tuple(
        f"#{row[0]} {_clip(row[1])}".rstrip() for row in result.all()
    )


async def _counts_by_subject(
    session: AsyncSession, model
) -> dict[int, int]:
    result = await session.execute(
        select(model.subject_id, func.count()).group_by(model.subject_id)
    )
    return {
        int(subject_id): int(count) for subject_id, count in result.all()
    }


async def list_subject_dependency_summaries(
    session: AsyncSession,
) -> dict[int, SubjectDependencySummary]:
    counts_by_model = {}
    subject_ids: set[int] = set()
    for key, model in _SUMMARY_MODELS:
        counts = await _counts_by_subject(session, model)
        counts_by_model[key] = counts
        subject_ids.update(counts)

    return {
        subject_id: SubjectDependencySummary(
            **{
                key: counts_by_model[key].get(subject_id, 0)
                for key, _ in _SUMMARY_MODELS
            }
        )
        for subject_id in subject_ids
    }


async def inspect_subject_for_removal(
    session: AsyncSession, subject_id: int
) -> tuple[SubjectRemovalPreview | None, str | None]:
    if subject_id <= 0:
        return None, "Subject ID must be a positive number."

    result = await session.execute(
        select(Subject)
        .where(Subject.id == subject_id)
        .execution_options(populate_existing=True)
    )
    subject = result.scalar_one_or_none()
    if subject is None:
        return None, f"Subject #{subject_id} was not found."

    references = []
    for label, model, label_column in _SAMPLE_REFERENCES:
        count = await _reference_count(session, model, subject_id)
        samples = (
            await _reference_samples(
                session, model, label_column, subject_id
            )
            if count
            else ()
        )
        references.append(SubjectReference(label, count, samples))
    for label, model in _COUNT_ONLY_REFERENCES:
        references.append(
            SubjectReference(
                label, await _reference_count(session, model, subject_id)
            )
        )

    return (
        SubjectRemovalPreview(
            subject_id=subject.id,
            name=subject.name,
            slug=subject.slug,
            references=tuple(references),
        ),
        None,
    )


async def delete_subject_if_unreferenced(
    session: AsyncSession, subject_id: int, admin_id: int
) -> tuple[SubjectRemovalPreview | None, str | None]:
    preview, error = await inspect_subject_for_removal(session, subject_id)
    if error or preview is None:
        return preview, error
    if preview.has_dependencies:
        return (
            preview,
            f"Subject #{subject_id} still has linked records. Nothing was deleted.",
        )

    checks = [
        ~select(Course.id)
        .where(Course.subject_id == subject_id)
        .exists(),
        ~select(Lecture.id)
        .where(Lecture.subject_id == subject_id)
        .exists(),
        ~select(Teacher.id)
        .where(Teacher.subject_id == subject_id)
        .exists(),
        ~select(TeacherAlias.id)
        .where(TeacherAlias.subject_id == subject_id)
        .exists(),
        ~select(Topic.id)
        .where(Topic.subject_id == subject_id)
        .exists(),
        ~select(SubjectFollower.id)
        .where(SubjectFollower.subject_id == subject_id)
        .exists(),
        ~select(PinnedSubject.id)
        .where(PinnedSubject.subject_id == subject_id)
        .exists(),
    ]
    result = await session.execute(
        delete(Subject)
        .where(Subject.id == subject_id, *checks)
        .execution_options(synchronize_session=False)
    )
    if (result.rowcount or 0) != 1:
        await session.rollback()
        refreshed, refreshed_error = await inspect_subject_for_removal(
            session, subject_id
        )
        if refreshed is not None:
            return (
                refreshed,
                "Subject references changed before deletion. Nothing was deleted.",
            )
        return preview, refreshed_error or "Subject was not deleted."

    await session.commit()
    await record_admin_action(
        session,
        admin_id,
        "remove_subject",
        target_type="subject",
        target_id=subject_id,
        details=f"{preview.name} ({preview.slug})",
    )
    session.expire_all()
    return preview, None
