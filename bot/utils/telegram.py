from __future__ import annotations


def chat_matches(setting: str, chat) -> bool:
    if not setting:
        return False
    target = setting.strip()
    if target.startswith("@"):
        return (getattr(chat, "username", None) or "").casefold() == target[1:].casefold()
    return str(getattr(chat, "id", None)) == target
