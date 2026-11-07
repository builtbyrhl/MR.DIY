from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import get_settings
from bot.keyboards.inline import (
    access_group_kb,
    access_review_kb,
    request_access_kb,
)
from bot.services.access import (
    APPROVED,
    PENDING,
    REJECTED,
    access_expires_at,
    access_is_expired,
    access_status,
    approve_access,
    display_name,
    format_access_expiry,
    has_library_access,
    list_expiring_access,
    list_pending,
    notify_admins,
    reject_access,
    request_access,
    revoke_access,
)
from bot.services.auth import is_admin
from bot.services.media_delivery import retention_hours
from bot.services.onboarding import (
    ACCESS_GROUP_NAME,
    INVITE_AVAILABLE,
    INVITE_EXPIRED,
    INVITE_FAILED,
    create_access_invite,
    invite_state,
)

from bot.db.models import User
from bot.db.repo import record_admin_action

router = Router()
logger = logging.getLogger(__name__)

PENDING_HEADER = "Pending access requests:"


def _is_admin_event(event) -> bool:
    sender = getattr(event, "from_user", None)
    return sender is not None and is_admin(sender.id)


async def _pending_view(session) -> tuple[str, InlineKeyboardMarkup | None]:
    users = await list_pending(session)
    expiring = await list_expiring_access(session)
    if not users and not expiring:
        return "No pending access requests.", None

    lines = []
    rows = []
    if users:
        lines.extend([PENDING_HEADER, ""])
        for user in users:
            lines.append(f"• {display_name(user)}")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ Approve {user.id}",
                        callback_data=f"access:approve:{user.id}",
                    ),
                    InlineKeyboardButton(
                        text=f"❌ Reject {user.id}",
                        callback_data=f"access:reject:{user.id}",
                    ),
                ]
            )
        lines.append(
            "\nUse /approveuser <id> [days], /rejectuser <id>, "
            "or /revokeuser <id>."
        )

    if expiring:
        if lines:
            lines.append("")
        lines.append("Access expiring in the next 7 days:")
        for user in expiring:
            lines.append(
                f"• {display_name(user)} — "
                f"{format_access_expiry(access_expires_at(user))}"
            )
        lines.append(
            "Use /approveuser <id> <days> to renew or extend access."
        )

    return "\n".join(lines), (
        InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    )


async def _refresh_pending_message(cb: CallbackQuery, session) -> None:
    text, keyboard = await _pending_view(session)
    try:
        await cb.message.edit_text(
            text, reply_markup=keyboard, parse_mode=None
        )
    except Exception:
        logger.exception("Could not refresh pending access list")


async def _notify_decision(
    bot, user, *, approved: bool, revoked: bool = False
) -> bool:
    if approved:
        text = "✅ Your library access has been approved.\n\n"
        expires = access_expires_at(user)
        if expires is not None:
            text += (
                f"Access is valid until {format_access_expiry(expires)}.\n\n"
            )
        invite = await create_access_invite(bot, user.id)
        if invite.link:
            text += (
                f"Use the single-use invite below to join the "
                f"{ACCESS_GROUP_NAME} student group."
            )
            markup = access_group_kb(invite.link)
        else:
            text += (
                f"The {ACCESS_GROUP_NAME} invitation could not be created "
                "right now. Access is still approved; use /myaccess to retry."
            )
            markup = access_group_kb(retry=True)
    elif revoked:
        text = "❌ Your library access has been revoked by an admin."
        markup = None
    else:
        text = (
            "❌ Your library access request was not approved. "
            "Contact an admin if you believe this is a mistake."
        )
        markup = None
    try:
        await bot.send_message(
            user.id, text, reply_markup=markup, parse_mode=None
        )
        return True
    except Exception:
        logger.warning("Could not notify user %s", user.id, exc_info=True)
        return False


def _parse_target_id(message: Message) -> int | None:
    parts = (message.text or "").split()
    if len(parts) != 2:
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    return user_id if user_id > 0 else None


def _parse_approval_target(
    message: Message,
) -> tuple[int | None, int | None]:
    parts = (message.text or "").split()
    if len(parts) not in {2, 3}:
        return None, None
    try:
        user_id = int(parts[1])
        days = int(parts[2]) if len(parts) == 3 else None
    except ValueError:
        return None, None
    if user_id <= 0 or (days is not None and days < 0):
        return None, None
    return user_id, days


@router.message(Command("access"))
async def access_list_cmd(message: Message, session):
    if not _is_admin_event(message):
        return
    text, keyboard = await _pending_view(session)
    await message.answer(text, reply_markup=keyboard, parse_mode=None)


@router.message(Command("approveuser"))
async def approve_user_cmd(message: Message, session, bot):
    if not _is_admin_event(message):
        return
    user_id, days = _parse_approval_target(message)
    if user_id is None:
        await message.answer("Usage: /approveuser <user_id> [days]", parse_mode=None)
        return
    existing = await session.get(User, user_id)
    was_active = existing is not None and has_library_access(existing)
    user = await approve_access(
        session,
        user_id,
        message.from_user.id,
        create_missing=True,
        days=days,
    )
    if was_active and days is None:
        await record_admin_action(
            session,
            message.from_user.id,
            "approve_access",
            target_type="user",
            target_id=user_id,
            details="already active",
        )
        await message.answer(
            f"User {user_id} already has active library access; "
            "the expiry was not changed.",
            parse_mode=None,
        )
        return
    notified = await _notify_decision(bot, user, approved=True)
    await record_admin_action(
        session,
        message.from_user.id,
        "approve_access",
        target_type="user",
        target_id=user_id,
        details=None if days is None else f"days={days}",
    )
    suffix = "" if notified else " The user could not be notified."
    if days is None:
        expires = access_expires_at(user)
        duration = (
            f" until {format_access_expiry(expires)}"
            if expires is not None
            else ""
        )
    elif days == 0:
        duration = " with lifetime access"
    else:
        duration = f" for {days} days"
    await message.answer(
        f"Approved library access for {user_id}{duration}.{suffix}",
        parse_mode=None,
    )


@router.message(Command("rejectuser"))
async def reject_user_cmd(message: Message, session, bot):
    if not _is_admin_event(message):
        return
    user_id = _parse_target_id(message)
    if user_id is None:
        await message.answer("Usage: /rejectuser <user_id>", parse_mode=None)
        return
    user = await reject_access(session, user_id, message.from_user.id)
    if user is None:
        await message.answer(f"User {user_id} was not found.", parse_mode=None)
        return
    notified = await _notify_decision(bot, user, approved=False)
    await record_admin_action(
        session,
        message.from_user.id,
        "reject_access",
        target_type="user",
        target_id=user_id,
    )
    suffix = "" if notified else " The user could not be notified."
    await message.answer(
        f"Rejected library access for {user_id}.{suffix}",
        parse_mode=None,
    )


@router.message(Command("revokeuser"))
async def revoke_user_cmd(message: Message, session, bot):
    if not _is_admin_event(message):
        return
    user_id = _parse_target_id(message)
    if user_id is None:
        await message.answer("Usage: /revokeuser <user_id>", parse_mode=None)
        return
    if is_admin(user_id):
        await message.answer("Cannot revoke access for an administrator.")
        return
    user = await revoke_access(session, user_id, message.from_user.id)
    if user is None:
        await message.answer(f"User {user_id} was not found.", parse_mode=None)
        return
    notified = await _notify_decision(
        bot, user, approved=False, revoked=True
    )
    await record_admin_action(
        session,
        message.from_user.id,
        "revoke_access",
        target_type="user",
        target_id=user_id,
    )
    suffix = "" if notified else " The user could not be notified."
    await message.answer(
        f"Revoked library access for {user_id}.{suffix}",
        parse_mode=None,
    )


def _active_invite_view(db_user):
    settings = get_settings()
    ttl = max(1, min(getattr(settings, "access_invite_ttl_minutes", 10), 60))
    if not settings.access_group_id.strip():
        return (
            f"\n\n⚠️ {ACCESS_GROUP_NAME} is not configured. "
            "Contact an admin.",
            None,
        )

    state = invite_state(db_user.id)
    if state.status == INVITE_AVAILABLE and state.link:
        return (
            f"\n\n🔗 Your single-use {ACCESS_GROUP_NAME} invitation is "
            f"available until {format_access_expiry(state.expires_at)}.",
            access_group_kb(state.link),
        )
    if state.status == INVITE_EXPIRED:
        return (
            f"\n\n⚠️ Your previous {ACCESS_GROUP_NAME} invitation expired. "
            "Generate a new one below.",
            access_group_kb(retry=True),
        )
    if state.status == INVITE_FAILED:
        return (
            f"\n\n⚠️ Your last {ACCESS_GROUP_NAME} invitation attempt failed. "
            "Try again below.",
            access_group_kb(retry=True),
        )
    return (
        f"\n\n🔗 No active invitation is recorded. Your invitation is "
        f"single-use and expires after {ttl} minutes; generate one below "
        "if needed.",
        access_group_kb(),
    )


def my_access_view(db_user):
    status = access_status(db_user)
    expires = access_expires_at(db_user)
    retention = retention_hours()

    if is_admin(db_user.id):
        text = "🔑 *My Access*\n\nAdministrator access (no expiry)."
        markup = None
    elif status == APPROVED and not access_is_expired(db_user):
        text = "🔑 *My Access*\n\n✅ Library access is active."
        if expires is not None:
            text += (
                f"\nValid until {format_access_expiry(expires)}."
            )
        else:
            text += "\nNo expiry date."
        invite_text, markup = _active_invite_view(db_user)
        text += invite_text
    elif status == APPROVED and access_is_expired(db_user):
        text = (
            "🔑 *My Access*\n\n"
            f"⏳ Your library access expired on "
            f"{format_access_expiry(expires)}.\n\n"
            "Request a renewal below to receive a new "
            f"{ACCESS_GROUP_NAME} invitation."
        )
        markup = request_access_kb()
    elif status == PENDING:
        if getattr(db_user, "access_requested_at", None):
            text = (
                "🔑 *My Access*\n\n"
                "⏳ Your access request is awaiting admin review."
            )
            markup = request_access_kb(pending=True)
        else:
            text = (
                "🔑 *My Access*\n\n"
                "🔒 Library access has not been approved yet."
            )
            markup = request_access_kb()
    elif status == REJECTED:
        text = (
            "🔑 *My Access*\n\n"
            "❌ Your access request was not approved.\n\n"
            "You can request access again."
        )
        markup = request_access_kb()
    else:
        text = (
            "🔑 *My Access*\n\n"
            "🔒 Library access has not been approved yet."
        )
        markup = request_access_kb()

    text += (
        f"\n\nLecture files sent to you are automatically removed "
        f"after {retention} hours where Telegram permits."
    )
    return text, markup


@router.message(Command("myaccess"))
@router.message(F.text == "🔑 My Access")
async def my_access_cmd(message: Message, db_user):
    text, markup = my_access_view(db_user)
    await message.answer(
        text, parse_mode="Markdown", reply_markup=markup
    )


@router.callback_query(F.data == "request_access")
async def request_access_cb(cb: CallbackQuery, session, db_user, bot):
    if has_library_access(db_user):
        await cb.answer("You already have library access.")
        return

    status = access_status(db_user)
    if status == PENDING and db_user.access_requested_at is not None:
        try:
            await cb.message.edit_text(
                "⏳ Your access request is already pending.", parse_mode=None
            )
        except Exception:
            logger.exception("Could not update access request message")
        await cb.answer("Request pending")
        return

    created = await request_access(session, db_user.id)
    await session.refresh(db_user)
    status = access_status(db_user)

    if created:
        await notify_admins(
            bot,
            f"📥 New library access request\n{display_name(db_user)}",
            reply_markup=access_review_kb(db_user.id),
        )

    if status == PENDING:
        text = (
            "✅ Access request sent. An admin will review it. If approved, "
            f"you'll receive a single-use {ACCESS_GROUP_NAME} invitation."
            if created
            else "⏳ Your access request is already pending."
        )
        try:
            await cb.message.edit_text(text, parse_mode=None)
        except Exception:
            logger.exception("Could not update access request message")
        await cb.answer("Request pending" if not created else "Request sent")
        return

    if status == APPROVED:
        await cb.answer("Your access is already approved. Use /start.")
        return
    await cb.answer("Your access request could not be created. Use /start.")


@router.callback_query(F.data == "access:invite")
async def access_invite_cb(cb: CallbackQuery, db_user, bot):
    if not has_library_access(db_user):
        await cb.answer(
            "Library access is not approved. Use /start.", show_alert=True
        )
        return

    invite = await create_access_invite(bot, db_user.id)
    if not invite.link:
        await cb.answer(
            invite.error or "The invitation could not be created. Try again.",
            show_alert=True,
        )
        return

    try:
        await cb.message.edit_text(
            f"🔗 Your single-use {ACCESS_GROUP_NAME} invite is ready.",
            reply_markup=access_group_kb(invite.link),
            parse_mode=None,
        )
    except Exception:
        logger.exception("Could not show Med Lounge invite")
    await cb.answer(f"{ACCESS_GROUP_NAME} invitation created.")


@router.callback_query(F.data.startswith("access:"))
async def access_decision_cb(cb: CallbackQuery, session, bot):
    if not _is_admin_event(cb):
        await cb.answer("Admin only.", show_alert=True)
        return

    try:
        _, action, raw_user_id = cb.data.split(":", 2)
        user_id = int(raw_user_id)
        if user_id <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await cb.answer("Invalid access request.", show_alert=True)
        return

    was_active = False
    if action == "approve":
        existing = await session.get(User, user_id)
        was_active = existing is not None and has_library_access(existing)
        user = await approve_access(
            session, user_id, cb.from_user.id, create_missing=False
        )
        decision = "approved"
    elif action == "reject":
        user = await reject_access(session, user_id, cb.from_user.id)
        decision = "rejected"
    else:
        await cb.answer("Unknown action.", show_alert=True)
        return

    if user is None:
        await cb.answer("User not found.", show_alert=True)
        return

    if was_active:
        await record_admin_action(
            session,
            cb.from_user.id,
            "approve_access",
            target_type="user",
            target_id=user_id,
            details="already active",
        )
        message_text = getattr(cb.message, "text", None) or ""
        if message_text.startswith(PENDING_HEADER):
            await _refresh_pending_message(cb, session)
        else:
            await cb.message.edit_text(
                f"Access already active for {display_name(user)}.",
                parse_mode=None,
            )
        await cb.answer("Access already active.")
        return

    notified = await _notify_decision(
        bot, user, approved=(action == "approve")
    )
    await record_admin_action(
        session,
        cb.from_user.id,
        "approve_access" if action == "approve" else "reject_access",
        target_type="user",
        target_id=user_id,
        details="via access button",
    )

    message_text = getattr(cb.message, "text", None) or ""
    if message_text.startswith(PENDING_HEADER):
        await _refresh_pending_message(cb, session)
    else:
        try:
            suffix = "" if notified else " (user could not be notified)"
            await cb.message.edit_text(
                f"Access {decision} for {display_name(user)}.{suffix}",
                parse_mode=None,
            )
        except Exception:
            logger.exception("Could not update access decision message")

    await cb.answer(f"Access {decision}.")
