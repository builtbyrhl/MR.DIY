from __future__ import annotations

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 Library"), KeyboardButton(text="▶️ Continue")],
            [KeyboardButton(text="📈 Progress"), KeyboardButton(text="⚙️ Settings")],
        ],
        resize_keyboard=True,
    )
