from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.handlers.channel import is_stale_join


def _event(when):
    return SimpleNamespace(date=when)


def test_fresh_join_is_not_stale():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert is_stale_join(_event(now - timedelta(seconds=30)), now=now) is False


def test_old_join_is_stale():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert is_stale_join(_event(now - timedelta(hours=3)), now=now) is True


def test_naive_date_is_treated_as_utc():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    when = datetime(2026, 5, 1, 11, 0)
    assert is_stale_join(_event(when), now=now) is True


def test_missing_date_is_not_stale():
    assert is_stale_join(_event(None)) is False
