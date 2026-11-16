from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

MRDIY_TOKEN_ENV = "BOT_TOKEN"
BRAINEXE_TOKEN_ENV = "BRAINEXE_BOT_TOKEN"
BOT_IDENTITIES = frozenset({"mrdiy", "brainexe"})
BOT_DISPLAY_NAMES = {"mrdiy": "MR.DIY", "brainexe": "Brain.exe"}
BOT_TAGLINES = {
    "mrdiy": "Your organised medical lecture library.",
    "brainexe": "Your brain's favourite operating system.",
}
BOT_EMOJIS = {"mrdiy": "🎓", "brainexe": "🧠"}


class ConfigurationError(ValueError):
    """Raised when bot configuration is missing or invalid."""


@dataclass
class Settings:
    bot_identity: str = field(default_factory=lambda: (
        os.getenv("BOT_IDENTITY", "mrdiy").strip().lower() or "mrdiy"
    ))
    mrdiy_bot_token: str = field(default_factory=lambda: os.getenv(
        MRDIY_TOKEN_ENV, ""
    ).strip())
    brainexe_bot_token: str = field(default_factory=lambda: os.getenv(
        BRAINEXE_TOKEN_ENV, ""
    ).strip())
    admin_ids: set[int] = field(default_factory=lambda: {
        int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
    })
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/mrdiy"
    ))
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    channel_id: str = field(default_factory=lambda: os.getenv("CHANNEL_ID", ""))  # source channel for lectures, not onboarding
    upload_inbox_chat_id: str = field(default_factory=lambda: os.getenv("UPLOAD_INBOX_CHAT_ID", ""))
    destination_group_id: str = field(default_factory=lambda: os.getenv("DESTINATION_GROUP_ID", ""))
    access_group_id: str = field(default_factory=lambda: (
        os.getenv("ACCESS_GROUP_ID") or os.getenv("DESTINATION_GROUP_ID", "")
    ))
    access_invite_ttl_minutes: int = field(default_factory=lambda: int(os.getenv("ACCESS_INVITE_TTL_MINUTES", "10")))
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", ""))
    webhook_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", "secret"))
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8080")))
    premium_price_inr: int = field(default_factory=lambda: int(os.getenv("PREMIUM_PRICE_INR", "499")))
    razorpay_key_id: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_ID", ""))
    razorpay_key_secret: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_SECRET", ""))
    free_lectures_per_day: int = field(default_factory=lambda: int(os.getenv("FREE_LECTURES_PER_DAY", "3")))
    access_default_days: int = field(default_factory=lambda: int(os.getenv("ACCESS_DEFAULT_DAYS", "0")))
    media_retention_hours: int = field(default_factory=lambda: int(os.getenv("MEDIA_RETENTION_HOURS", "24")))
    media_cleanup_interval_seconds: int = field(default_factory=lambda: int(os.getenv("MEDIA_CLEANUP_INTERVAL_SECONDS", "300")))
    reminder_hour_utc: int = field(default_factory=lambda: int(os.getenv("REMINDER_HOUR_UTC", "14")))
    digest_weekday: int = field(default_factory=lambda: int(os.getenv("DIGEST_WEEKDAY", "6")))
    digest_hour_utc: int = field(default_factory=lambda: int(os.getenv("DIGEST_HOUR_UTC", "14")))

    def __post_init__(self) -> None:
        self.bot_identity = self.bot_identity.strip().lower() or "mrdiy"
        if self.bot_identity not in BOT_IDENTITIES:
            allowed = ", ".join(sorted(BOT_IDENTITIES))
            raise ConfigurationError(
                f"Invalid BOT_IDENTITY={self.bot_identity!r}. "
                f"Set BOT_IDENTITY to one of: {allowed}."
            )
        if not self.selected_bot_token:
            raise ConfigurationError(
                f"Missing {self.bot_token_env} for "
                f"BOT_IDENTITY={self.bot_identity}. "
                f"Set {self.bot_token_env} in the environment."
            )

    @property
    def bot_token_env(self) -> str:
        return (
            MRDIY_TOKEN_ENV
            if self.bot_identity == "mrdiy"
            else BRAINEXE_TOKEN_ENV
        )

    @property
    def selected_bot_token(self) -> str:
        return (
            self.mrdiy_bot_token
            if self.bot_identity == "mrdiy"
            else self.brainexe_bot_token
        )

    @property
    def bot_token(self) -> str:
        return self.selected_bot_token

    @property
    def token_configured(self) -> bool:
        return bool(self.selected_bot_token)

    @property
    def bot_display_name(self) -> str:
        return BOT_DISPLAY_NAMES[self.bot_identity]

    @property
    def bot_tagline(self) -> str:
        return BOT_TAGLINES[self.bot_identity]

    @property
    def bot_brand_emoji(self) -> str:
        return BOT_EMOJIS[self.bot_identity]

    @property
    def bot_identity_summary(self) -> str:
        configured = "yes" if self.token_configured else "no"
        return (
            f"profile={self.bot_identity} "
            f"display_name={self.bot_display_name} "
            f"token_source={self.bot_token_env} "
            f"token_configured={configured}"
        )

    @property
    def is_webhook(self) -> bool:
        return bool(self.webhook_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
