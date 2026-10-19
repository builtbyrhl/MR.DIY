from __future__ import annotations

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 Subjects"), KeyboardButton(text="🔍 Search")],
            [KeyboardButton(text="⭐ Bookmarks"), KeyboardButton(text="📈 Progress")],
            [KeyboardButton(text="💎 Premium"), KeyboardButton(text="❓ Help")],
        ],
        resize_keyboard=True,
    )
