from __future__ import annotations

import base64
import json

import httpx
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command

from bot.config import get_settings
from bot.db.repo import is_premium, create_payment

router = Router()


PREMIUM_TEXT = (
    "💎 *Premium Plan*\n\n"
    "• HD quality lectures\n"
    "• Download for offline\n"
    "• Notes & summaries\n"
    "• All quizzes unlocked\n"
    "• No daily limits\n\n"
    "Price: ₹{price}/year"
)


@router.message(F.text == "💎 Premium")
async def premium_entry(message: Message, session, db_user):
    await show_premium(message, session, db_user)


@router.message(Command("premium"))
async def premium_cmd(message: Message, session, db_user):
    await show_premium(message, session, db_user)


async def show_premium(message_or_cb, session, db_user):
    settings = get_settings()
    premium = await is_premium(db_user)
    text = PREMIUM_TEXT.format(price=settings.premium_price_inr)
    if premium:
        text += "\n\n✅ You already have Premium!"
        kb = None
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Pay ₹{settings.premium_price_inr}", callback_data="pay")],
        ])
    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message_or_cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "premium")
async def premium_cb(cb: CallbackQuery, session, db_user):
    await show_premium(cb, session, db_user)
    await cb.answer()


@router.callback_query(F.data == "pay")
async def pay_cb(cb: CallbackQuery, session, db_user, bot):
    settings = get_settings()
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        await cb.answer("Payments not configured. Contact admin.", show_alert=True)
        return
    # create razorpay order
    auth = base64.b64encode(f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()).decode()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.razorpay.com/v1/orders",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            content=json.dumps({
                "amount": settings.premium_price_inr * 100,
                "currency": "INR",
                "receipt": f"u{db_user.id}",
                "notes": {"user_id": str(db_user.id)},
            }),
        )
    if r.status_code != 200:
        await cb.answer("Payment init failed", show_alert=True)
        return
    order = r.json()
    await create_payment(session, db_user.id, settings.premium_price_inr, order["id"])
    link = f"https://rzp.io/l/{order['id']}"  # placeholder; real link via checkout
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Open payment page", url=f"https://api.razorpay.com/v1/checkout/embedded/ab?order_id={order['id']}&key={settings.razorpay_key_id}")],
    ])
    await cb.message.edit_text(
        f"Order created: `{order['id']}`\nComplete payment; premium activates automatically.",
        parse_mode="Markdown", reply_markup=kb,
    )
    await cb.answer()
