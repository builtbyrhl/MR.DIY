from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers import review
from bot.handlers.review import _authorized
from bot.services.auth import is_admin, require_admin


def _message(user_id, text=""):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


def test_admin_ids_are_recognized():
    assert is_admin(123) is True
    assert is_admin(999) is False


def test_require_admin_raises_for_non_admin():
    require_admin(123)
    with pytest.raises(PermissionError):
        require_admin(999)


def test_review_commands_gate_unauthorized_users():
    assert _authorized(_message(123)) is True
    assert _authorized(_message(999)) is False


@pytest.mark.parametrize(
    "handler,text",
    [
        (review.review_cmd, "/review"),
        (review.batch_cmd, "/batch 1"),
        (review.dryrun_cmd, "/dryrun 1"),
        (review.approve_cmd, "/approve 1"),
        (review.unlockpublish_cmd, "/unlockpublish 1"),
        (review.skip_cmd, "/skip 1"),
        (review.keep_cmd, "/keep 1"),
        (review.retry_cmd, "/retry 1"),
        (review.setcourse_cmd, "/setcourse 1 1"),
        (review.setnumber_cmd, "/setnumber 1 1"),
        (review.pause_cmd, "/pause"),
        (review.resume_cmd, "/resume"),
        (review.addcourse_cmd, "/addcourse 1st Year|anatomy|Dr A|dr a"),
        (review.courses_cmd, "/courses"),
    ],
)
async def test_every_review_command_rejects_non_admin(handler, text, session):
    message = _message(999, text)
    await handler(message, session)
    message.answer.assert_not_awaited()
