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

from bot.keyboards.inline import access_review_kb
from bot.keyboards.reply import main_menu_kb
from bot.services.access import (
    APPROVED,
    PENDING,
    access_status,
    approve_access,
    display_name,
    has_library_access,
    list_pending,
    notify_admins,
    reject_access,
    request_access,
    revoke_access,
)
from bot.services.auth import is_admin

router = Router()
logger = logging.getLogger(__name__)

PENDING_HEADER = "Pending access requests:"


def _is_admin_event(event) -> bool:
    sender = getattr(event, "from_user", None)
    return sender is not None and is_admin(sender.id)


async def _pending_view(session) -> tuple[str, InlineKeyboardMarkup | None]:
    users = await list_pending(session)
    if not users:
        return "No pending access requests.", None

    lines = [PENDING_HEADER, ""]
    rows = []
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
    lines.append("\nUse /approveuser <id>, /rejectuser <id>, or /revokeuser <id>.")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


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
        text = (
            "✅ Your library access has been approved.\n\n"
            "Open the menu below to start studying."
        )
        markup = main_menu_kb()
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
    user_id = _parse_target_id(message)
    if user_id is None:
        await message.answer("Usage: /approveuser <user_id>")
        return
    user = await approve_access(
        session, user_id, message.from_user.id, create_missing=True
    )
    notified = await _notify_decision(bot, user, approved=True)
    suffix = "" if notified else " The user could not be notified."
    await message.answer(
        f"Approved library access for {user_id}.{suffix}",
        parse_mode=None,
    )


@router.message(Command("rejectuser"))
async def reject_user_cmd(message: Message, session, bot):
    if not _is_admin_event(message):
        return
    user_id = _parse_target_id(message)
    if user_id is None:
        await message.answer("Usage: /rejectuser <user_id>")
        return
    user = await reject_access(session, user_id, message.from_user.id)
    if user is None:
        await message.answer(f"User {user_id} was not found.", parse_mode=None)
        return
    notified = await _notify_decision(bot, user, approved=False)
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
        await message.answer("Usage: /revokeuser <user_id>")
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
    suffix = "" if notified else " The user could not be notified."
    await message.answer(
        f"Revoked library access for {user_id}.{suffix}",
        parse_mode=None,
    )


@router.callback_query(F.data == "request_access")
async def request_access_cb(cb: CallbackQuery, session, db_user, bot):
    if has_library_access(db_user):
        await cb.answer("You already have library access.")
        return

    created = await request_access(session, db_user.id)
    await session.refresh(db_user)
    status = access_status(db_user)

    if created:
        await notify_admins(
            bot,
            f"🔔 New library access request\n{display_name(db_user)}",
            reply_markup=access_review_kb(db_user.id),
        )

    if status == PENDING:
        text = (
            "⏳ Access request sent. An admin will review your request."
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

    if action == "approve":
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

    notified = await _notify_decision(
        bot, user, approved=(action == "approve")
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
