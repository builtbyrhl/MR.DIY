from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.db.models import AdminUser, User
from bot.db.repo import (
    add_teacher,
    add_teacher_alias,
    find_matching_teachers,
    list_teacher_aliases,
    remove_teacher_alias,
    rename_teacher,
)
from bot.handlers import admin, review
from bot.services.auth import is_admin, load_db_admin_cache


def _private_message(user_id, text):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(type="private"),
        text=text,
        answer=AsyncMock(),
    )


async def test_teacher_alias_matches_without_fuzzy_spelling(session, seeded_course):
    subject, _ = seeded_course
    teacher, error = await add_teacher(
        session, subject.id, "Dr. Muhammad Azam"
    )
    assert error is None
    assert teacher is not None

    assert (
        await find_matching_teachers(session, subject.id, "Dr Mohammad Azam")
        == []
    )

    alias, error = await add_teacher_alias(
        session, teacher.id, "Dr Mohammad Azam"
    )
    assert error is None
    assert alias is not None

    matches = await find_matching_teachers(
        session, subject.id, "Dr Mohammad Azam"
    )
    assert [match.id for match in matches] == [teacher.id]
    aliases = await list_teacher_aliases(session, teacher.id)
    assert [row.name for row in aliases] == ["Dr Mohammad Azam"]


async def test_add_teacher_with_aliases_and_collision_validation(
    session, seeded_course
):
    subject, _ = seeded_course
    teacher, error = await add_teacher(
        session,
        subject.id,
        "Dr. Muhammad Azam",
        aliases=["Mohammad Azam", "Dr Mohammad Azam", "Mohammad Azam"],
    )
    assert error is None
    assert teacher is not None
    aliases = await list_teacher_aliases(session, teacher.id)
    assert {row.name for row in aliases} == {
        "Mohammad Azam",
        "Dr Mohammad Azam",
    }

    _, error = await add_teacher(
        session, subject.id, "Another Teacher", aliases=["Mohammad Azam"]
    )
    assert error is not None
    assert "already registered" in error

    _, error = await add_teacher(
        session, subject.id, "Third Teacher", aliases=["Dr. Muhammad Azam"]
    )
    assert error is not None
    assert "already the name" in error


async def test_teacher_rename_preserves_id_and_links(session, seeded_course):
    subject, _ = seeded_course
    teacher, _ = await add_teacher(session, subject.id, "Dr. Muhammad Azam")
    assert teacher is not None
    await add_teacher_alias(session, teacher.id, "Dr Mohammad Azam")

    renamed, error = await rename_teacher(
        session, teacher.id, "Dr. Muhammad Azam Sir"
    )
    assert error is None
    assert renamed is not None
    assert renamed.id == teacher.id
    assert renamed.name == "Dr. Muhammad Azam Sir"

    matches = await find_matching_teachers(
        session, subject.id, "Dr Mohammad Azam"
    )
    assert [match.id for match in matches] == [teacher.id]

    removed, error = await remove_teacher_alias(
        session, teacher.id, "Dr Mohammad Azam"
    )
    assert error is None
    assert removed is True
    assert (
        await find_matching_teachers(session, subject.id, "Dr Mohammad Azam")
        == []
    )


async def test_addteacher_command_accepts_aliases(session, seeded_course):
    subject, _ = seeded_course
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text=(
            "/addteacher anatomy|Dr. Muhammad Azam|"
            "Dr Mohammad Azam,Mohammad Azam"
        ),
        answer=AsyncMock(),
    )

    await review.addteacher_cmd(message, session)

    answer = message.answer.await_args.args[0]
    assert "Added teacher" in answer
    matches = await find_matching_teachers(
        session, subject.id, "Dr Mohammad Azam"
    )
    assert len(matches) == 1

    teachers_message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        text="/teachers anatomy",
        answer=AsyncMock(),
    )
    await review.teachers_cmd(teachers_message, session)
    listing = teachers_message.answer.await_args.args[0]
    assert "Dr. Muhammad Azam" in listing
    assert "Dr Mohammad Azam" in listing


async def test_owner_can_add_and_remove_database_admin(session):
    session.add(User(id=456, full_name="New Admin"))
    await session.commit()

    assert is_admin(456) is False

    add_message = _private_message(123, "/addadmin 456")
    await admin.addadmin_cmd(add_message, session)

    assert "Granted administrator access" in add_message.answer.await_args.args[0]
    assert is_admin(456) is True
    assert await session.get(AdminUser, 456) is not None
    assert (await session.get(User, 456)).is_admin is True

    list_message = _private_message(123, "/admins")
    await admin.admins_cmd(list_message, session)
    listing = list_message.answer.await_args.args[0]
    assert "Owners" in listing
    assert "456" in listing

    remove_message = _private_message(123, "/removeadmin 456")
    await admin.removeadmin_cmd(remove_message, session)

    assert "Removed administrator" in remove_message.answer.await_args.args[0]
    assert is_admin(456) is False
    assert await session.get(AdminUser, 456) is None
    assert (await session.get(User, 456)).is_admin is False


async def test_database_admin_cannot_manage_admins_and_owners_are_protected(
    session,
):
    session.add_all(
        [
            User(id=456, full_name="New Admin"),
            User(id=789, full_name="Another User"),
        ]
    )
    await session.commit()

    await admin.addadmin_cmd(_private_message(123, "/addadmin 456"), session)
    assert is_admin(456) is True

    denied = _private_message(456, "/addadmin 789")
    await admin.addadmin_cmd(denied, session)
    assert "Only owner admins" in denied.answer.await_args.args[0]
    assert is_admin(789) is False

    owner_remove = _private_message(123, "/removeadmin 123")
    await admin.removeadmin_cmd(owner_remove, session)
    assert "cannot be removed" in owner_remove.answer.await_args.args[0]
    assert is_admin(123) is True

    group_add = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(type="supergroup"),
        text="/addadmin 789",
        answer=AsyncMock(),
    )
    await admin.addadmin_cmd(group_add, session)
    assert "private chat" in group_add.answer.await_args.args[0]
    assert is_admin(789) is False


async def test_admin_cache_loads_database_admins(session):
    session.add(User(id=456, full_name="New Admin"))
    session.add(AdminUser(user_id=456, granted_by=123))
    await session.commit()

    assert is_admin(456) is False
    loaded = await load_db_admin_cache(session)
    assert 456 in loaded
    assert is_admin(456) is True
    assert is_admin(123) is True
