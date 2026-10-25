import os

os.environ["BOT_TOKEN"] = "test-token"
os.environ["ADMIN_IDS"] = "123"

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, Course, Subject
from bot.services.auth import set_db_admin_cache


@pytest.fixture(autouse=True)
def reset_db_admin_cache():
    set_db_admin_cache(set())
    yield
    set_db_admin_cache(set())


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, factory
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db):
    _, factory = db
    async with factory() as db_session:
        yield db_session


@pytest_asyncio.fixture
async def seeded_course(session):
    subject = Subject(slug="anatomy", name="Anatomy", emoji="🫀", order=1)
    session.add(subject)
    await session.commit()
    await session.refresh(subject)

    course = Course(
        year="",
        subject_id=subject.id,
        name="Dr A Anatomy",
        aliases="dr a,anatomy a",
    )
    session.add(course)
    await session.commit()
    await session.refresh(course)
    return subject, course
