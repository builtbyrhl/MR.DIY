from __future__ import annotations

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 My Courses"), KeyboardButton(text="🔍 Search")],
            [KeyboardButton(text="▶️ Continue"), KeyboardButton(text="🔑 My Access")],
            [KeyboardButton(text="❓ Help")],
        ],
        resize_keyboard=True,
    )
