"""Regression tests for institution hierarchy."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from bot.db.models import Course, Institution, Lecture, Subject, UploadBatch, UploadItem


@pytest.mark.asyncio
async def test_institution_migration_backfill_idempotent(session, monkeypatch):
    """Backfill creates default institution and is idempotent."""
    from bot.db import session as db_session

    # Run backfill multiple times
    await session.execute(__import__("sqlalchemy").text("DELETE FROM lectures"))
    await session.execute(__import__("sqlalchemy").text("DELETE FROM courses"))
    await session.execute(__import__("sqlalchemy").text("DELETE FROM upload_items"))
    await session.execute(__import__("sqlalchemy").text("DELETE FROM institutions"))

    # Use engine sync connection
    engine = db_session.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(db_session.ensure_institution_backfill)
        await conn.run_sync(db_session.ensure_institution_backfill)  # idempotent

    res = await session.execute(select(Institution))
    insts = res.scalars().all()
    assert len(insts) == 1
    assert insts[0].slug == "default-institution"


@pytest.mark.asyncio
async def test_default_institution_assignment_and_overrides(session, tmp_path, monkeypatch):
    from bot.db import ingestion_repo
    from bot.services import ingestion

    # Ensure default institution exists
    inst = Institution(name="Default", slug="default")
    session.add(inst)
    await session.commit()
    await session.refresh(inst)
    await ingestion_repo.set_setting(session, "default_institution_id", str(inst.id))

    # Create batch
    from datetime import date
    batch = UploadBatch(source_chat_id=1, batch_date=date.today(), status="open")
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    # Ingest should assign default institution
    item, created = await ingestion.ingest_media(
        session,
        source_chat_id=1,
        source_message_id=123,
        media_type="video",
        caption="Anat | Dr X | Head | Lec1 | L1",
        file_name="anat.mp4",
        file_unique_id="uniq1",
        file_id="file1",
    )
    assert created
    await session.refresh(item)
    assert item.institution_id == inst.id

    # Override via batch/item
    inst2 = Institution(name="Other", slug="other")
    session.add(inst2)
    await session.commit()
    await session.refresh(inst2)

    from sqlalchemy import update
    await session.execute(update(UploadItem).where(UploadItem.id == item.id).values(institution_id=inst2.id))
    await session.commit()
    await session.refresh(item)
    assert item.institution_id == inst2.id


@pytest.mark.asyncio
async def test_publishing_inherits_institution(session):
    from bot.services.catalog import project_upload_to_lecture
    from bot.db.models import Subject, Teacher, Course

    subject = Subject(slug="anat", name="Anatomy", emoji="🫀")
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    inst = Institution(name="Inst", slug="inst")
    session.add(inst)
    await session.commit()
    await session.refresh(inst)

    course = Course(subject_id=subject.id, institution_id=inst.id, year="1", name="Anat1")
    session.add(course)
    await session.commit()
    await session.refresh(course)

    batch = UploadBatch(source_chat_id=1, batch_date=__import__("datetime").date.today(), status="approved")
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    item = UploadItem(
        batch_id=batch.id,
        institution_id=inst.id,
        source_chat_id=1,
        source_message_id=999,
        media_type="video",
        title="L1",
        course_id=course.id,
        status="approved",
        publication_status="published",
        published_message_id=12345,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)

    lecture, _created = await project_upload_to_lecture(session, item)
    assert lecture is not None
    assert lecture.institution_id == inst.id
